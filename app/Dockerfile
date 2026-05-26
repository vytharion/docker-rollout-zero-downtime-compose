FROM python:3.12-slim

WORKDIR /srv

COPY server.py /srv/server.py

ENV APP_PORT=8000
EXPOSE 8000

CMD ["python", "-u", "server.py"]
