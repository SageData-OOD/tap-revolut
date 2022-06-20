#!/usr/bin/env python3
import os
import json
import backoff
import requests
import singer
from datetime import datetime, timedelta
from singer import utils, metadata
from singer.catalog import Catalog, CatalogEntry
from singer.schema import Schema
from singer.transform import transform

REQUIRED_CONFIG_KEYS = ["start_date", "access_token"]
LOGGER = singer.get_logger()
HOST = "https://b2b.revolut.com/api/1.0"
END_POINTS = {
    "accounts": "/accounts",
    "counterparties": "/counterparties",
    "transactions": "/transactions"
}

PAGE_RECORDS_LIMIT = 1000
FULL_TABLE_SYNC_STREAMS = ["accounts", "counterparties"]
INCREMENTAL_SYNC_STREAMS = ["transactions"]


class RevolutRateLimitError(Exception):
    def __init__(self, msg):
        self.msg = msg
        super().__init__(self.msg)


def get_key_properties(stream_id):
    return ["id"]


def get_bookmark(stream_id):
    """
    Bookmarks for the streams which has incremental sync.
    """
    bookmarks = {
        "transactions": "updated_at"
    }
    return bookmarks.get(stream_id)


def get_abs_path(path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), path)


def load_schemas():
    """ Load schemas from schemas folder """
    schemas = {}
    for filename in os.listdir(get_abs_path('schemas')):
        path = get_abs_path('schemas') + '/' + filename
        file_raw = filename.replace('.json', '')
        with open(path) as file:
            schemas[file_raw] = Schema.from_dict(json.load(file))
    return schemas


def create_metadata_for_report(stream_id, schema, key_properties):
    replication_key = get_bookmark(stream_id)
    mdata = [{"breadcrumb": [], "metadata": {"inclusion": "available", "forced-replication-method": "FULL_TABLE", "selected": True}}]

    if key_properties:
        mdata[0]["metadata"]["table-key-properties"] = key_properties

    if stream_id in INCREMENTAL_SYNC_STREAMS:
        mdata[0]["metadata"]["forced-replication-method"] = "INCREMENTAL"
        mdata[0]["metadata"]["valid-replication-keys"] = [replication_key]

    for key in schema.properties:
        # hence, when property is object, we will only consider properties of that object without taking object itself.
        if "object" in schema.properties.get(key).type and schema.properties.get(key).properties:
            inclusion = "available"
            mdata.extend(
                [{"breadcrumb": ["properties", key, "properties", prop], "metadata": {"inclusion": inclusion, "selected": True}} for prop
                 in schema.properties.get(key).properties])
        else:
            inclusion = "automatic" if key in key_properties + [replication_key] else "available"
            mdata.append({"breadcrumb": ["properties", key], "metadata": {"inclusion": inclusion, "selected": True}})

    return mdata


def discover():
    raw_schemas = load_schemas()
    streams = []
    for stream_id, schema in raw_schemas.items():
        stream_metadata = create_metadata_for_report(stream_id, schema, get_key_properties(stream_id))
        key_properties = get_key_properties(stream_id)
        streams.append(
            CatalogEntry(
                tap_stream_id=stream_id,
                stream=stream_id,
                schema=schema,
                key_properties=key_properties,
                metadata=stream_metadata
            )
        )
    return Catalog(streams)


def requests_session(session=None):
    """
    Creates or configures an HTTP session to use retries
    Returns:
        The configured HTTP session object
    """
    session = session or requests.Session()
    return session


@backoff.on_exception(backoff.expo, RevolutRateLimitError, max_tries=5, factor=2)
@utils.ratelimit(60, 60)
def make_request(session, url, parameters, headers):
    response = session.get(url, headers=headers, params=parameters)

    if response.status_code == 429:
        raise RevolutRateLimitError(response.text)
    elif response.status_code != 200:
        raise Exception(response.text)

    return response


def request_data(tap_stream_id, headers, parameters, session=None):
    url = HOST + END_POINTS[tap_stream_id]
    session = requests_session(session)

    response = make_request(session, url, parameters, headers)
    res = response.json()
    return res


def datetime_to_str(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def get_next_date(_date: str):
    return datetime_to_str(datetime.strptime(_date, '%Y-%m-%dT%H:%M:%SZ') + timedelta(days=1))


def get_verified_date_to_poll(stream_id, date_to_poll):
    """
    For "transactions", API only allow access to the last 90 days’ transactions.
    so, if date_to_poll is less than current date, it will be changed to the least possible date value (today - 90 days)
    """
    if stream_id == "transactions":
        minimal_date_to_poll = datetime_to_str(datetime.utcnow() - timedelta(days=90))
        date_to_poll = max(date_to_poll, minimal_date_to_poll)

    return date_to_poll


def sync_incremental(config, state, stream):
    bookmark_column = get_bookmark(stream.tap_stream_id)
    mdata = metadata.to_map(stream.metadata)
    schema = stream.schema.to_dict()

    singer.write_schema(
        stream_name=stream.tap_stream_id,
        schema=schema,
        key_properties=stream.key_properties,
    )
    headers = {"accept": "application/json",
               "Authorization": "Bearer " + config["access_token"]}
    start_date = singer.get_bookmark(state, stream.tap_stream_id, bookmark_column) \
        if state.get("bookmarks", {}).get(stream.tap_stream_id) \
        else config["start_date"]
    session = requests_session()
    today = datetime_to_str(datetime.utcnow().date())

    bookmark = get_verified_date_to_poll(stream.tap_stream_id, start_date)
    while True:
        next_date = get_next_date(bookmark)
        params = {
            "from": bookmark,
            "to": next_date,
            "count": PAGE_RECORDS_LIMIT,
        }
        LOGGER.info("Querying Date --> from: %s, to: %s ", bookmark, next_date)
        tap_data = request_data(stream.tap_stream_id, headers, params, session=session)
        with singer.metrics.record_counter(stream.tap_stream_id) as counter:
            for row in tap_data:
                # Type Conversation and Transformation
                transformed_data = transform(row, schema, metadata=mdata)

                # write one or more rows to the stream:
                singer.write_records(stream.tap_stream_id, [transformed_data])
                counter.increment()
                bookmark = max([bookmark, row[bookmark_column]])

        state = singer.write_bookmark(state, stream.tap_stream_id, bookmark_column, bookmark)
        singer.write_state(state)

        if bookmark <= today:
            bookmark = next_date
        if bookmark > today:
            break


def sync_full_table(config, state, stream):
    mdata = metadata.to_map(stream.metadata)
    schema = stream.schema.to_dict()

    singer.write_schema(
        stream_name=stream.tap_stream_id,
        schema=schema,
        key_properties=stream.key_properties,
    )
    headers = {"accept": "application/json",
               "Authorization": "Bearer " + config["access_token"]}
    session = requests_session()

    tap_data = request_data(stream.tap_stream_id, headers, parameters={}, session=session)

    with singer.metrics.record_counter(stream.tap_stream_id) as counter:
        for row in tap_data:
            # Type Conversation and Transformation
            transformed_data = transform(row, schema, metadata=mdata)

            # write one or more rows to the stream:
            singer.write_records(stream.tap_stream_id, [transformed_data])
            counter.increment()


def sync(config, state, catalog):
    # Loop over selected streams in catalog
    for stream in catalog.get_selected_streams(state):
        LOGGER.info("Syncing stream:" + stream.tap_stream_id)

        if stream.tap_stream_id in INCREMENTAL_SYNC_STREAMS:
            sync_incremental(config, state, stream)
        else:
            sync_full_table(config, state, stream)
    return


@utils.handle_top_exception(LOGGER)
def main():
    # Parse command line arguments
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)

    if args.discover:
        catalog = discover()
        catalog.dump()
    else:
        if args.catalog:
            catalog = args.catalog
        else:
            catalog = discover()
        sync(args.config, args.state, catalog)


if __name__ == "__main__":
    main()

