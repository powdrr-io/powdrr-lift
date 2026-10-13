FROM powdrr-science-gql-base:f07c89f8
RUN pip install basedpyright==1.39.9 PyYAML jsonschema
COPY src /opt/powdrr/src
COPY worker.py /opt/planning/repository_planning/worker.py
COPY symbols.py /opt/planning/repository_planning/symbols.py
ENV PYTHONPATH=/opt/powdrr/src:/opt/planning PYTHONDONTWRITEBYTECODE=1
CMD ["sleep", "infinity"]
