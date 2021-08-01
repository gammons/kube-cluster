FROM alpine
RUN apk add --no-cache aws-cli
RUN mkdir /app
WORKDIR /app
COPY backup.sh /app/backup.sh
