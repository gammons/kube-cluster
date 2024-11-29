# Unifi controller

- kc apply -f ns.yml
- kc apply -f pvc.yml
- kc apply -f deployment.yml

Double-check the metallb deployment.  by default it uses 192.168.20.1

Unifi seems to run on a node port, so go to https://192.168.10.2:8443/
