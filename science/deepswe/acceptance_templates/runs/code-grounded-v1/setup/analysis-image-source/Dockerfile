FROM powdrr-science-gql-base:f07c89f8
RUN pip install basedpyright==1.39.9 PyYAML jsonschema
COPY src /opt/powdrr/src
COPY worker.py /opt/planning/worker.py
ENV PYTHONPATH=/opt/powdrr/src PYTHONDONTWRITEBYTECODE=1
CMD ["sleep", "infinity"]
