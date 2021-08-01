#!/bin/bash

_now=`date +"%Y-%m-%d-%H"`
_file="minecraft-buckaroo-town-$_now.tgz"
tar -czvf $_file /data/*.json /data/server.properties /data/world
aws s3 cp "$_file" s3://minecraft-buckaroo-town-backup/"$_file"
