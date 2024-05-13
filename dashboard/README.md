# Kube dashboard

1. Install dashboard from [instructions][1]
1. Run the dashboard.yml file to create the admin user.
1. kubectl get secret admin-user -n kubernetes-dashboard -o jsonpath={".data.token"} | base64 -d > ~/token2.txt

You can access the dashboard by running:

```
kubectl -n kubernetes-dashboard port-forward svc/kubernetes-dashboard-kong-proxy 8443:443
```

Then going to https://localhost:8443

[1]: https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/
