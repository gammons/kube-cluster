#!/bin/bash

_now=`date +"%Y-%m-%d-%H"`
_file="minecraft-$1-$_now.tgz"
tar -czvf $_file /data/*.json /data/server.properties /data/world
aws s3 cp "$_file" s3://minecraft-$1-backup/"$_file"
