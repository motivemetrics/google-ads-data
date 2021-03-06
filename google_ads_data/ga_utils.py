#!/usr/bin/env python
# encoding: utf-8
"""
Copyright (c) 2022 MotiveMetrics. All rights reserved.

Utilities for making Google Ads API queries.

To make a Google Ads API query, we need to get a refresh token
and possibly a customerId from the AppX mongodb database.

With that we can derive the authorizing account's customerId
and get a GoogleAdsService instance with which to make a query.
"""

import boto3
import datetime
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.api_core.retry import Retry
from google.protobuf.json_format import MessageToDict
import pandas
import pytz
import re
import typing
import yaml

from .account_utils import cust_id_to_refresh_token

GOOGLE_ADS_API_VERSION = "v10"
CUSTOMER_CLIENT_QUERY = """
    SELECT
      customer_client.status,
      customer_client.client_customer,
      customer_client.manager,
      customer_client.id
    FROM customer_client
    WHERE customer_client.status='ENABLED'
"""


def make_base_ga_config_dict(refreshToken):
    client = boto3.client("ssm")
    response = client.get_parameter(Name="keys_google_adwords_api_keys.yml")
    keys = yaml.safe_load(response["Parameter"]["Value"])
    configDict = dict(
        refresh_token=refreshToken,
        client_id=keys["client_id"],
        client_secret=keys["client_secret"],
        developer_token=keys["developer_token"],
        use_proto_plus=True,
    )
    return configDict


def get_login_customer_id(custId, refreshToken):
    configDict = make_base_ga_config_dict(refreshToken)

    # get customerIds for all top-level accounts accessible by the
    # refreshToken
    client = GoogleAdsClient.load_from_dict(configDict)

    customerService = client.get_service(
        "CustomerService", version=GOOGLE_ADS_API_VERSION
    )
    resourceNames = customerService.list_accessible_customers().resource_names
    directIds = [rn.replace("customers/", "") for rn in resourceNames]
    # if custId is a top-level account for this refreshToken, then assume
    # it is also the login customerId
    if custId in directIds:
        return custId

    # the account we want data for is a sub-account of the authorizing
    # account, so we need to find out which of the top-level accounts is
    # the parent
    googleAdsService = client.get_service(
        "GoogleAdsService", version=GOOGLE_ADS_API_VERSION
    )
    for parentId in directIds:
        try:
            response = googleAdsService.search(
                customer_id=parentId,
                query=CUSTOMER_CLIENT_QUERY,
                retry=Retry(maximum=8, deadline=15),
            )
        except GoogleAdsException:
            continue

        for result in response.results:
            if result.customer_client.id == int(custId):
                return parentId

    return None


def get_ga_api_service(custId, serviceName):
    refreshToken = cust_id_to_refresh_token(custId)
    if refreshToken is None:
        return None

    configDict = make_base_ga_config_dict(refreshToken)
    configDict["login_customer_id"] = get_login_customer_id(custId, refreshToken)
    client = GoogleAdsClient.load_from_dict(configDict)
    service = client.get_service(serviceName, version=GOOGLE_ADS_API_VERSION)
    return service


def camel_to_snake(camelString):
    pattern = re.compile(r"(?<!^)(?=[A-Z])")
    return pattern.sub("_", camelString).lower()


def snake_to_camel(snakeString):
    temp = snakeString.split("_")
    return temp[0] + "".join(s.capitalize() for s in temp[1:])


def get_nested_dict_value(key, d):
    keys = key.split(".")
    for k in keys:
        if k not in d:
            return None

        d = d.get(k)

    return d


def account_time(custId: str) -> datetime.datetime:
    """
    Returns a timezone-aware datetime that represents the current
    time in the account's timezone

    :arg custId:
        ``customer.id`` for the Google Ads account

    :return:
        Account's current time
    """
    service = get_ga_api_service(custId, "GoogleAdsService")
    query = "SELECT customer.time_zone FROM customer"
    response = service.search(
        customer_id=custId, query=query, retry=Retry(maximum=8, deadline=15)
    )
    row = list(response)[0]
    tz = pytz.timezone(row.customer.time_zone)
    return datetime.datetime.utcnow().astimezone(tz)


def account_date(custId: str) -> datetime.date:
    """
    Returns the current date for an account, based on the account's timezone

    :arg custId:
        ``customer.id`` for the Google Ads account

    :return:
        Account's current date.
    """
    return account_time(custId).date()


def make_base_query(
    custId: str,
    fromResource: str,
    fields: typing.List[str],
    start: typing.Union[datetime.date, datetime.datetime] = None,
    end: typing.Union[datetime.date, datetime.datetime] = None,
    zeroImpressions: bool = False,
) -> str:
    """
    Make a basic Google Ads Query Language (GAQL) query to be used with
    `GoogleAdsService.SearchStream` or `GoogleAdsService.Search`

    :arg custId:
        The Google Ads ``customer.id`` resource for the account.

    :arg fromResource:
        The Google Ads API resource that fields will be selected from.
        For example ``keyword_view``

    :arg fields:
        The Google Ads API resource fields that you want to return data
        for. For example ``['campaign.name', 'metrics.impressions']``

    :arg start:
        Start date for metrics. Defaults to the current day for the
        specified customer account

    :arg end:
        End date for metrics. Defaults to the current day for the
        specified customer account

    :arg zeroImpressions:
        Whether to include resources with zero impressions. Default is
        False.

    :return:
        A fully formed GAQL query.
    """
    today = account_date(custId)
    if start is None:
        start = today

    if isinstance(start, datetime.datetime):
        start = start.date()

    if end is None:
        end = today

    if isinstance(end, datetime.datetime):
        end = end.date()

    query = "SELECT "
    query += ", ".join(fields)
    query += f" FROM {fromResource} "

    wheres = []
    if zeroImpressions is False:
        wheres.append("metrics.impressions > 0")

    startstr = start.strftime("%Y-%m-%d")
    wheres.append(f"segments.date >= '{startstr}'")

    endstr = end.strftime("%Y-%m-%d")
    wheres.append(f"segments.date <= '{endstr}'")

    whereclause = " AND ".join(wheres)
    query += f" WHERE {whereclause}"

    return query


def execute_query(
    custId: str, query: str, fields: typing.List[str]
) -> pandas.DataFrame:
    """
    Execute a GAQL query using ``GoogleAdsService.SearchStream``
    and return the results in a pandas DataFrame

    :arg custId:
        The Google Ads ``customer.id`` resource for the account.

    :arg query:
        A fully-formed GAQL query.

    :arg fields:
        The Google Ads API resource fields that are selected in the query

    :return:
        A pandas DataFrame with data for each of the requested fields.
    """
    camelFields = [snake_to_camel(f) for f in fields]

    service = get_ga_api_service(custId, "GoogleAdsService")
    stream = service.search_stream(customer_id=custId, query=query)

    rows = []
    for batch in stream:
        for r in batch.results:
            rDict = MessageToDict(r._pb)
            row = []
            for f in camelFields:
                row.append(get_nested_dict_value(f, rDict))

            rows.append(row)

    df = pandas.DataFrame(rows, columns=fields)
    return df


def get_ga_data(
    custId: str,
    fromResource: str,
    fields: typing.List[str],
    start: typing.Union[datetime.date, datetime.datetime] = None,
    end: typing.Union[datetime.date, datetime.datetime] = None,
    zeroImpressions: bool = False,
    wheres: typing.List[str] = [],
) -> pandas.DataFrame:
    """
    Get a pandas dataframe of Google Ads data for a customer account

    :arg custId:
        The Google Ads ``customer.id`` resource for the account.

    :arg fromResource:
        The Google Ads API resource that fields will be selected from.
        For example ``keyword_view``

    :arg fields:
        The Google Ads API resource fields that you want to return data
        for. For example ``['campaign.name', 'metrics.impressions']``

    :arg start:
        Start date for metrics. Defaults to the current day for the
        specified customer account

    :arg end:
        End date for metrics. Defaults to the current day for the
        specified customer account

    :arg zeroImpressions:
        Whether to include resources with zero impressions. Default is
        False.

    :arg wheres:
        Additional 'WHERE' clauses to add to the GAQL query. For example
        ``ad_group_ad.ad.type = 'RESPONSIVE_SEARCH_AD'``

    :return:
        A pandas DataFrame with data for each of the requested fields.
    """
    query = make_base_query(custId, fromResource, fields, start, end, zeroImpressions)

    if wheres:
        query += " AND " + " AND ".join(wheres)

    df = execute_query(custId, query, fields)
    return df
