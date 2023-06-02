#!/bin/bash

_now=`date +"%Y-%m-%d-%H"`
_file="minecraft-$1-$_now.tgz"
tar -czvf $_file /data/Baci\ SMP
aws s3 cp "$_file" s3://minecraft-$1-backup/"$_file"
