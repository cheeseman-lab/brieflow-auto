# One verified entrance to the execution workspace

The setup workstation provisions the machine and stages inputs. The execution machine
hosts Claude Code, its shell/notebook tools, the environment, and the pipeline. A remote
connection is access to that machine, not a remote pipeline dispatcher. Models remain
served by their provider. Do not run the analysis by sending pipeline commands over SSH.

Keep three identities separate: the client's local username, the cloud identity that
authorizes access, and the remote Linux account that owns the workspace. Never infer the
third from the first two. Different people retain their own credentials; this workflow
does not share accounts or bypass an organization's identity policy.

## GCP: resolve once, save, verify

After provisioning, use the actual instance name and successful zone, not today's date
or the originally requested zone. Ask which cloud account the operator will use on both
clients. It must already be authenticated in gcloud on each client. Do not change global
gcloud defaults or enable service-account impersonation as a workaround.

From the plugin directory on the setup workstation:

```bash
python scripts/brieflow_connection.py gcp \
  --project example-project --zone us-central1-a --instance screen-vm \
  --account operator@example.org --workdir /data/analysis/example-analysis \
  --output connection.json
```

The helper reads instance/project metadata with the specified project and account.
Instance OS Login settings take precedence over project settings. With OS Login enabled,
the assigned username comes from the account's OS Login profile. Missing or ambiguous
profiles stop planning. An explicit `--remote-user` must match a returned Linux account.
With OS Login disabled, `--remote-user` is required: ask for the intended existing account,
or explicitly agree on the account for a new machine. Do not guess from email or `$USER`.
An inaccessible metadata/profile lookup is a blocker, not permission to assume another mode.

Planning only saves a local, unverified record. It does not SSH or change IAM, keys, users,
or resources. After authorization to establish the connection, verify:

```bash
python scripts/brieflow_connection.py verify --record connection.json
```

Verification opens one IAP SSH connection. gcloud may create/register this client's SSH
key and bootstrap the explicitly selected account; it is not a purely local operation.
Verification does not answer interactive prompts. If key creation, host-key confirmation
or multi-factor authentication requires interaction, use the printed connection command
interactively first, then retry verification. Never disable those checks. Some managed
policies may prevent noninteractive verification; report that limitation rather than success.
The remote probe only reads username, UID/GID, home, hostname, and directory ownership/
access. It does not install software, create directories, change ownership, or launch work.
It requires remote Python 3. It also rechecks the GCP instance ID and authentication mode,
so a recreated machine is not silently accepted as the previous destination.

Before first boot, the intended analysis directory may not exist. A successful identity
probe then reports `identity-verified-directory-blocked`, not setup completion. The operator
can use the printed connection to finish setup on the machine as that account. Verify again
after the analysis directory exists and is readable, writable and traversable.

The record keeps the verified remote identity and path, not private keys or tokens. Preserve
it locally with the screen's setup notes and give the operator a copy for their other client.
Do not overwrite it to accommodate a login mismatch; resolve access or create a deliberately
new record for a changed destination. It is private setup context, not a public artifact.

## Connect from a laptop or another workstation

On either client, use `show` to print the same explicit connection:

```bash
python scripts/brieflow_connection.py show --record connection.json
```

The printed gcloud command pins remote user, account, project, zone, instance and IAP.
It can be copied verbatim without installing the plugin on the second client. Install and
authenticate gcloud there using the recorded cloud account; retain that client's own SSH
keys. Re-run verification from that client before claiming its access works. A successful
check on the HPC does not establish that a laptop has the required credentials or network.

Prefer this command over a generated direct-IP SSH alias. For VS Code Remote SSH, configure
the same remote User and IAP route, and test the entry on that client; local key paths and
gcloud executable paths are client-specific. Never copy private keys between clients or
disable host-key verification to make a connection work.

If OS Login assigns a different username for a different cloud identity, stop and explain
the mismatch. Do not create a second workspace, run recursive chown, or switch identity policy.
An administrator must resolve access where necessary.

## Existing non-GCP machine

No provisioning integration is needed. Record the existing hostname (or configured SSH
alias), port, explicitly assigned Linux user, and analysis directory:

```bash
python scripts/brieflow_connection.py ssh --host server.example.org --port 22 \
  --remote-user operator --workdir /srv/analysis/example-analysis --output connection.json
python scripts/brieflow_connection.py verify --record connection.json
```

Existing SSH configuration supplies keys, bastion routing and host verification. The helper
does not modify it. A configured alias must exist on each client using it. This is ordinary
remote access followed by on-machine execution, not support for another job scheduler.

## Runtime handoff and persistence

Deliver the connection record plus screen slug, transfer status, input locations, setup
stage, and environment name. The generated instructions show the remote `cd` and a named
tmux session. Authenticate Claude Code on that machine as the operator. Start it inside
tmux, or reconnect to the existing session rather than starting a duplicate process.
The operator can detach and reconnect without keeping the laptop awake. tmux does not
survive a stopped/rebooted VM; use saved run state to recover after host loss.

Before declaring setup complete, confirm the remote account and analysis directory through
verification, then check the environment, plugin, screen description and inputs on the
execution machine. The helper verifies access, not complete pipeline readiness. Do not
automatically attach an existing unrelated tmux session or launch the pipeline.

Sources: [GCP OS Login](https://docs.cloud.google.com/compute/docs/oslogin/set-up-oslogin),
[profile lookup](https://docs.cloud.google.com/sdk/gcloud/reference/compute/os-login/describe-profile),
[gcloud SSH](https://docs.cloud.google.com/sdk/gcloud/reference/compute/ssh).
