import re
import requests
import sys
import os
import urllib
import json
import datetime
import time
import psycopg2
from prometheus_client import start_http_server, Gauge


# dictionary of service, SLO query pairs
# platform, service, metric, query
SLO_querys = {}


def main():
    start_http_server(8000)
    g = Gauge("delta_slo", "Least performant service", ["service", "metric"])

    with open("/config/SLO_config.json") as slo_config:
        data = json.load(slo_config)
        services = data["SLO_Queries"]
        for service in services:
            service_name = service["service"]
            SLO_querys[service_name] = {}
            queries = service["queries"]
            for query_object in queries:
                metric = query_object["metric"]
                query = query_object["query"]
                target_slo = float(query_object["target_slo"])

                SLO_querys[service_name][metric] = {
                    "query": query,
                    "target_slo": target_slo
                }

    auth_token = os.environ.get('AUTH_TOKEN')

    try:
        connection = connect_db(60)

        connection.set_session(autocommit=True)

        # Create a cursor to perform database operations
        cursor = connection.cursor()
        # Print PostgreSQL details
        print("PostgreSQL server information")
        # Executing a SQL query
        cursor.execute("SELECT version();")
        # Fetch result
        record = cursor.fetchone()
        print("You are connected to - ", record, "\n")

        create_tables(connection)
    except (Exception, psycopg2.Error) as error:
        print("Error while using db connection", error)

    while(True):
        max_delta = {"service": "", "metric": "", "delta": 0}

        for service in SLO_querys.keys():
            for metric_key in SLO_querys[service].keys():
                service_slo = process_SLO(service, metric_key, connection, auth_token)
                if service_slo:
                    # Failure rate vs Success Rate require different comparisons. Decided by assumption that services should have a >50% success rate
                    if SLO_querys[service][metric_key]["target_slo"] > 0.5:
                        delta_slo = SLO_querys[service][metric_key]["target_slo"] - service_slo
                    else:
                        delta_slo = service_slo - SLO_querys[service][metric_key]["target_slo"]
                    print(delta_slo)
                    if delta_slo > max_delta["delta"]:
                        max_delta = {"service": service, "metric": metric_key, "delta": delta_slo}

        print(f"Worst performer is {max_delta['service']}, {max_delta['metric']} with a delta of {max_delta['delta']}")
        #h.observe(amount=max_delta['delta'], exemplar={"service": max_delta['service'], "metric": max_delta['metric']})
        g.labels(service=max_delta['service'], metric=max_delta['metric']).set(max_delta['delta'])

        # run every 10 min
        time.sleep(600)


def connect_db(retry_interval):
    while(True):
        try:
            connection = psycopg2.connect(  user=os.environ.get('DATABASE_USER'),
                                                password = os.environ.get('DATABASE_PASSWORD'),
                                                host = os.environ.get('POSTGRES_SQL_SERVICE_HOST'),
                                                port = os.environ.get('POSTGRES_SQL_SERVICE_PORT'),
                                                database = os.environ.get('DATABASE_NAME'))

            return connection
        except Exception as error:
            print("Error while connecting to metrics-db", error)
            print(f"Retyring connection in {retry_interval} seconds")
        
        time.sleep(retry_interval)


def create_tables(connection):
    try:
        cursor = connection.cursor()
        create_table_query = '''CREATE TABLE SLO
            (
            SERVICE           TEXT    NOT NULL,
            datetime          TIMESTAMP,
            SLO_name          TEXT,
            SLO_value         DOUBLE PRECISION); '''

        cursor.execute(create_table_query)
        connection.commit()
    except psycopg2.errors.DuplicateTable as error:
        return

def build_headers(auth_token):
    headers = {
        'authority': 'prometheus.crcs02ue1.devshift.net',
        'accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9',
        'accept-language': 'en-US,en;q=0.9',
        'cookie': auth_token,
        'sec-ch-ua': '"Chromium";v="104", " Not A;Brand";v="99", "Google Chrome";v="104"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Linux"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'none',
        'sec-fetch-user': '?1',
        'upgrade-insecure-requests': '1',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/104.0.0.0 Safari/537.36',
    }

    return headers

def process_SLO(service, metric, connection, auth_token):
    cursor = connection.cursor()

    SLO_dict = collect_SLO(service, metric, auth_token)
    if not SLO_dict:
        return None

    service_name = SLO_dict['service']
    slo_datetime = SLO_dict['datetime']
    slo_name = SLO_dict['SLO_name']
    slo_value = SLO_dict['SLO']

    cursor.execute('insert into SLO values(%s, %s, %s, %s)', (service_name, slo_datetime, slo_name, slo_value))
    return slo_value


def collect_SLO(service, metric, auth_token):
    
    try:
        query = SLO_querys[service][metric]["query"]
    except:
        print(f"Service {service} doesn't have a query assigned.")
        return None

    url = f'https://prometheus.crcs02ue1.devshift.net/api/v1/query?query={query}'
    headers = build_headers(auth_token)
    
    # For some reason the request will fail if the query is passed in through 'params'
    # url = "https://prometheus.crcs02ue1.devshift.net/api/v1/query"
    # query = {"query": "sum(rate(api_3scale_gateway_api_status{status=%225xx%22}[8h]))/sum(rate(api_3scale_gateway_api_status[8h]))"}
    # response = requests.get(url=url, params=query, headers=headers)
    
    response = requests.get(url=url, headers=headers)
    try:
        response_json = response.json()
        print(f"url = {response.url}\nresponse = {response_json}")

        if len(response_json['data']['result']) == 0:
            print(f"No data for query:{service}, {query}")
            return None
        else:
            SLO_value = response_json['data']['result'][0]['value'][1]
        
        return {
            'service': service,
            'datetime': datetime.datetime.now(),
            'SLO_name': metric,
            'SLO': float(SLO_value),
            'target_slo': SLO_querys[service][metric]["target_slo"]
        }
    except:
        print("Bad response from prometheus")
        return None


if __name__ == "__main__":
    main()