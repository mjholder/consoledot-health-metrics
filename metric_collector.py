import re
import requests
import sys
import os
import urllib
import json
import datetime
import dateutil.parser
import time
import psycopg2
from prometheus_client import start_http_server, Gauge
from pdpyras import APISession


# dictionary of service, SLO query pairs
# platform, service, metric, query
SLO_querys = {}


def main():
    start_http_server(8000)
    slo_gauge = Gauge("delta_slo", "Least performant service", ["service", "metric"])
    deployment_success_gauge = Gauge("deployment_success", "Number of successfull deployments in rolling 30 day period", ["app_name"])
    deployment_failure_gauge = Gauge("deployment_failure", "Number of failed deployments in rolling 30 day period", ["app_name"])
    time_to_resolution_gauge = Gauge("time_to_resolution", "Rolling 30 day average time to resolution")
    
    configure_SLO_querys()

    auth_token = os.environ.get('PROMETHEUS_AUTH_TOKEN')

    # Connect to db and initialize tables
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

    # Main processing loop
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
                    if delta_slo > max_delta["delta"]:
                        max_delta = {"service": service, "metric": metric_key, "current_slo": service_slo, "delta": delta_slo}

        print(f"Worst performer is {max_delta['service']}, {max_delta['metric']} with a delta of {max_delta['delta']}")
        slo_gauge.labels(service=max_delta['service'], metric=max_delta['metric']).set(max_delta['current_slo'])

        apps = configure_deployment_tracker()
        deployment_data = collect_deployments(apps)
        for deployment in deployment_data:
            deployment_success_gauge.labels(app_name=deployment).set(deployment_data[deployment]['successes'])
            deployment_failure_gauge.labels(app_name=deployment).set(deployment_data[deployment]['failures'])

        average_time_to_resolution = query_pagerduty()
        time_to_resolution_gauge.set(average_time_to_resolution / datetime.timedelta(minutes=1))

        # run every 10 min
        time.sleep(600)


def query_pagerduty():
    # PagerDuty setup
    api_key = os.environ['PD_API_KEY']
    until = datetime.datetime.now()
    since = until - datetime.timedelta(days=30)

    session = APISession(api_key, default_from="api@example-company.com")
    session.retry[500] = 4
    session.max_http_attempts = 4
    session.sleep_timer = 1
    session.sleep_timer_base = 2
    total_time_diff = datetime.timedelta()
    incident_count = 0

    for incident in session.iter_all('incidents', params={'statuses[]':['resolved'], 'since':since.isoformat(), 'until':until.isoformat(), 'team_ids[]':['PQQJJHI']}):
        print(f"{incident['title']} \ncreated on {incident['created_at']}\nresolved on {incident['last_status_change_at']}")
        time_diff = dateutil.parser.isoparse(incident['last_status_change_at']) - dateutil.parser.isoparse(incident['created_at'])
        total_time_diff += time_diff
        incident_count += 1
        
    average_time_to_resolution = total_time_diff/incident_count
    print(f"average time to resolution 30 days: {average_time_to_resolution}")
    return average_time_to_resolution
    


def configure_SLO_querys():
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

                
def configure_deployment_tracker():
    apps = {}
    with open("/config/deployment_config.json") as deploy_config:
        data = json.load(deploy_config)
        for app in data["apps"]:
            apps[app] = {"successes": 0, "failures": 0}

    return apps

def collect_deployments(apps):
    conn = psycopg2.connect(
        database=os.environ.get('DEPLOYMENT_DB_NAME'),
        user=os.environ.get('DEPLOYMENT_DB_USER'),
        host=os.environ.get('DEPLOYMENT_DB_HOST'),
        password=os.environ.get('DEPLOYMENT_DB_PASSWORD'),
    )

    cursor = conn.cursor()
    sql_query = (
                 'select deployment_time, succeeded, app_name, env_name\n'
                 'from deployments\n'
                f'where deployment_time >= current_date - {30}\n'
                 'and app_name in %s\n'
                 'and env_name = %s\n'
                 'order by app_name\n'
                )
    cursor.execute(sql_query, (tuple(apps), 'insights-production'))
    deployment_records = cursor.fetchall()

    for record in deployment_records:
        # successful deployment
        if record[1]:
            apps[record[2]]['successes'] += 1
        else:
            apps[record[2]]['failures'] += 1

    conn.close()
    return apps


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