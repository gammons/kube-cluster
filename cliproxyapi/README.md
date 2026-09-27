# CLIProxyAPI

Shared proxy for Claude/Anthropic-compatible clients, reachable on the LAN
(MetalLB) and on the tailnet (Tailscale operator).

## Apply

Create the real Secret first:

```bash
cp secret.example.yml secret.yml
# replace REPLACE_WITH_LONG_RANDOM_KEY with a long random value
kubectl --context local-k3s apply -f ns.yml
kubectl --context local-k3s apply -f pvc.yml
kubectl --context local-k3s apply -f secret.yml
kubectl --context local-k3s apply -f deployment.yml
kubectl --context local-k3s apply -f service.yml
kubectl --context local-k3s apply -f tailscale-service.yml
```

Do not commit `secret.yml`.

## Client endpoints

| Path | URL | Works from |
|------|-----|------------|
| Tailnet | `http://cliproxyapi.rya-scala.ts.net:8317/v1` | any device running Tailscale with MagicDNS, at home or away |
| LAN | `http://192.168.20.17:8317/v1` | the home network only |

Prefer the tailnet URL on machines that run Tailscale: it works both at home
and away, so the client config never needs switching. Traffic is encrypted by
WireGuard, so plain HTTP is fine on the tailnet.

In-cluster pods whose Tailscale sidecar is not their DNS resolver (for
example the dev box, which uses cluster DNS) cannot resolve `*.ts.net`
names. Use the LAN URL from those.

Use the API key from `secret.yml` as the bearer/API key.

## Add Claude accounts

Terminal 1:

```bash
kubectl --context local-k3s -n cliproxyapi port-forward deploy/cliproxyapi 54545:54545
```

Terminal 2:

```bash
kubectl --context local-k3s -n cliproxyapi exec -it deploy/cliproxyapi -- \
  /CLIProxyAPI/CLIProxyAPI -config /config/config.yaml -claude-login --no-browser
```

Open the printed OAuth URL in a browser. Repeat for each Claude subscription.

## Verify

```bash
KEY=$(awk '/- "/ {gsub(/"/, "", $2); print $2; exit}' secret.yml)
curl -fsS http://192.168.20.17:8317/healthz
curl -fsS http://192.168.20.17:8317/v1/models \
  -H "Authorization: Bearer $KEY"

# Tailnet path: the operator's proxy pod must be Ready, then the same checks work
kubectl --context local-k3s -n tailscale get pods \
  -l tailscale.com/parent-resource=cliproxyapi-tailscale
curl -fsS http://cliproxyapi.rya-scala.ts.net:8317/healthz
```

The `/v1/models` command returns Claude models after at least one successful Claude login.
