FROM centos/python-38-centos7
ADD Pipfile /opt/app-root/src/Pipfile
ADD Pipfile.lock /opt/app-root/src/Pipfile.lock
ENV PIP_DEFAULT_TIMEOUT=100
RUN pip install -U pip
RUN pip install -U pipenv
RUN pip install -U pipenv-to-requirements
RUN pipenv run pipenv_to_requirements -f
RUN pip install -U -r requirements.txt
USER root
WORKDIR /usr/app/src
COPY metric_collector.py ./
CMD ["python", "./metric_collector.py"]