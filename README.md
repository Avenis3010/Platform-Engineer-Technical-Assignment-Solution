# Cloud DevOps Engineer — Platform Engineer Technical Assignment

## Objective

This assignment demonstrates practical ownership of application deployments,
production troubleshooting, security, AWS infrastructure, and a small
application code change. The emphasis is on sound operational decisions and
clear implementation steps rather than advanced algorithms.

## Scenario and assumptions

The platform is an internal web application used by multiple teams. This
solution selects **Python with Flask** for the sample health endpoint and
assumes a Linux Ubuntu EC2 host, Nginx, Gunicorn, and PostgreSQL. In the
proposed AWS design, PostgreSQL is hosted on Amazon RDS.

The sample application currently contains only a health endpoint and does not
define business data models or a database schema. Accordingly, there is no real
migration command yet; the CI/CD and deployment workflow identify where an
application-specific migration hook must run once a schema exists.

The user's AWS account is suspended, so the AWS architecture and operational
commands below are a design and runbook, not a claim that AWS resources were
created or deployed. No actual credentials, keys, passwords, or confidential
company information are included.

## Part 1: Deployment approach

### Goal and deployment sequence

Deploy the application as a versioned release to a Linux server. Nginx accepts
web traffic and forwards application requests to Gunicorn. PostgreSQL should
only be reachable over a private network. A typical release proceeds in this
order:

1. Provision and harden the EC2 instance and its network rules.
2. Create a dedicated service account and application directories.
3. Configure secrets and environment variables outside the source repository.
4. Check out a reviewed commit, install dependencies into a release-specific
   virtual environment, and run tests.
5. Run any reviewed database migrations after backup and compatibility checks.
6. Point the active `current` symlink at the release and restart Gunicorn.
7. Verify the application directly and through Nginx/ALB.
8. If verification fails, restore the previous release and verify recovery.

### 1.1 Server setup and packages

Choose a supported Ubuntu LTS AMI, an appropriately sized EC2 instance, and a
VPC with public and private subnets as appropriate. Prefer placing application
instances in private subnets behind an Application Load Balancer. Apply OS
security updates, install only necessary packages, and use Systems Manager
Session Manager for administration if possible. If SSH is necessary, restrict
port 22 to approved administrator IP ranges and use key-based authentication.

Example initial package installation:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip nginx curl
```

Create a non-root account for the service and prepare the release directories:

```bash
sudo adduser --system --group --home /var/www/platform platform
sudo install -d -o platform -g www-data -m 0750 \
  /var/www/platform/releases /var/www/platform/shared
sudo install -d -o root -g www-data -m 0750 /etc/platform
```

The application should not run as root. PostgreSQL should not be installed
with a public listener for this design; use RDS in private subnets or a
separately managed private PostgreSQL service.

### 1.2 Application directory structure

Keep deployments immutable and isolated by commit:

```text
/var/www/platform/
├── current -> /var/www/platform/releases/<commit-sha>
├── releases/
│   ├── <current-commit-sha>/
│   │   ├── .venv/
│   │   ├── app.py
│   │   └── requirements.txt
│   └── <previous-commit-sha>/
└── shared/
    ├── static/
    └── media/
/etc/platform/platform.env
/run/platform/gunicorn.sock
```

`current` points at the release currently served. Release directories allow
fast code rollback without copying files back over an in-place installation.
Runtime configuration and persistent files must live outside release
directories. Store uploads and durable assets in S3 in the AWS architecture,
not on ephemeral EC2 storage.

### 1.3 Environment variables and secret management

Read configuration from the environment rather than hard-coding it. Typical
settings include:

- Framework-specific production/debug settings; for this Gunicorn deployment,
  do not enable Flask debug mode.
- `DATABASE_URL`, retrieved from a protected secret source.
- `HEALTHCHECK_DATABASE`, which controls the optional health endpoint database
  probe.
- Application-specific host, logging, and feature configuration.

Never commit secrets, private keys, credentials, or populated environment files
to the repository or release artifacts. Preferred AWS options are Secrets
Manager or Systems Manager Parameter Store with an EC2 instance role limited to
the exact parameters/secrets needed. Do not grant broad secret-list or
administrator permissions without a requirement.

If a local environment file is used, create it as root and restrict
permissions. Do not include a real password in documentation:

```bash
sudo install -o root -g www-data -m 0640 /dev/null \
  /etc/platform/platform.env
sudoedit /etc/platform/platform.env
```

Example contents use a placeholder:

```text
DATABASE_URL=<retrieve-from-secret-store>
HEALTHCHECK_DATABASE=false
```

The app should not log secret values. If a secret may have been exposed, rotate
it and investigate where it was used.

### 1.4 Dependency installation and pre-deployment checks

Install project dependencies into a virtual environment for the specific
release. Keep dependencies declared in `requirements.txt`, review updates, and
use pinned/locked versions for production deployments.

```bash
cd /var/www/platform/releases/<commit-sha>
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m unittest -v
```

The exact test command should match the project's test framework. The pipeline
must stop if dependency installation or tests fail; it must not promote an
untested commit.

### 1.5 Application server configuration

Run the Flask application with Gunicorn managed by `systemd`. Gunicorn should
run as the dedicated `platform` user and bind to a Unix socket, not an
internet-facing port. The sample unit is
[`deploy/platform.service`](deploy/platform.service). Its important settings
are:

- `User=platform`: avoids running application code as root.
- `WorkingDirectory=/var/www/platform/current`: uses the active release.
- `EnvironmentFile=/etc/platform/platform.env`: keeps secrets outside code.
- `RuntimeDirectory=platform`: lets systemd create `/run/platform`.
- `ExecStart=... gunicorn --bind unix:/run/platform/gunicorn.sock app:app`:
  starts the WSGI app through the Unix socket.
- `Restart=on-failure`: recovers from process failures.
- `UMask=0007`: prevents world access to the socket and created files.

Install and enable the unit:

```bash
sudo install -o root -g root -m 0644 deploy/platform.service \
  /etc/systemd/system/platform.service
sudo systemctl daemon-reload
sudo systemctl enable --now platform
sudo systemctl status platform --no-pager
```

Tune worker count and timeouts based on instance capacity and measured
workloads. Avoid selecting a worker count without load testing.

### 1.6 Nginx configuration

Nginx should be the public-facing reverse proxy. The sample
[`deploy/platform.nginx`](deploy/platform.nginx) proxies to Gunicorn's Unix
socket and forwards the host, client address, and protocol headers. Set the
`server_name` to the real application DNS name before use.

```bash
sudo install -o root -g root -m 0644 deploy/platform.nginx \
  /etc/nginx/sites-available/platform
sudo ln -s /etc/nginx/sites-available/platform \
  /etc/nginx/sites-enabled/platform
sudo nginx -t
sudo systemctl reload nginx
```

The provided Nginx sample listens on HTTP and assumes HTTPS is terminated at an
ALB. If TLS is terminated on the host instead, configure the certificate,
private key, TLS listener, and HTTP-to-HTTPS redirect securely. Do not expose
Gunicorn directly to the internet.

### 1.7 SSL certificate setup

In the recommended AWS architecture, request or import a certificate into AWS
Certificate Manager (ACM), validate control of the DNS name, and attach the
certificate to the ALB HTTPS listener. Configure HTTP to redirect to HTTPS and
restrict direct instance access so clients cannot bypass the ALB.

For a single-server/non-ALB example, a trusted certificate such as Let's
Encrypt can be installed using Certbot. Configure automated renewal and test
renewal before relying on it. Never use a self-signed certificate for
user-facing production traffic unless a controlled internal PKI is explicitly
used and trusted by all clients.

### 1.8 Application startup and restart process

`systemd` starts the application at boot and manages restarts:

```bash
sudo systemctl start platform
sudo systemctl status platform --no-pager
sudo systemctl restart platform
sudo systemctl reload nginx
```

Use `restart` for the Gunicorn service after application release changes.
Validate Nginx syntax with `nginx -t` before reloading Nginx. After a deploy,
check the local `/health` endpoint and then check the external HTTPS endpoint.

### 1.9 Log locations

Check application logs through the systemd journal:

```bash
sudo journalctl -u platform -n 100 --no-pager
sudo journalctl -u platform --since "30 minutes ago" --no-pager
```

On Ubuntu, Nginx logs are normally:

- Access log: `/var/log/nginx/access.log`
- Error log: `/var/log/nginx/error.log`

Review system/kernel events with `journalctl` when investigating OOM kills or
host-level issues. Configure log rotation, retention, access control, and
centralized CloudWatch collection in AWS. Avoid logging secrets or sensitive
user data.

### 1.10 Deployment rollback

Keep the last known-good release directory. Change `current` only after
dependency installation and any migrations have succeeded. Use an atomic
symlink replacement and restart the service. If restart or health verification
fails, switch the symlink back and restart the previous release.

The included [`deploy/deploy-platform`](deploy/deploy-platform) is a sample
server-side helper that accepts a commit archive, prepares a release, switches
the active symlink, restarts Gunicorn, and attempts rollback on a failed local
health check.

Example manual rollback:

```bash
sudo ln -sfn /var/www/platform/releases/<previous-commit-sha> \
  /var/www/platform/current
sudo systemctl restart platform
sudo systemctl status platform --no-pager
```

Verify health after rollback and retain logs from the failed release. A code
rollback does not necessarily undo a database migration. Use backward-
compatible migrations and restore a database snapshot only under an approved
recovery plan.

## Part 2: CI/CD pipeline

### Pipeline design

The example GitHub Actions pipeline is
[`.github/workflows/deploy.yml`](.github/workflows/deploy.yml). A push to
`main`—which occurs when a pull request is merged—starts the workflow. It also
supports a manual `workflow_dispatch`.

### Pipeline stages and steps

1. **Trigger and serialize deployments:** A push to `main` triggers the
   workflow. The `concurrency` group prevents overlapping production deploys.
2. **Check out source:** `actions/checkout` checks out the exact commit SHA
   that triggered the run.
3. **Set up Python:** Configure the same supported Python version used by the
   app.
4. **Install dependencies:** Install from `requirements.txt`.
5. **Run tests:** Run `python -m unittest -v`; a failure blocks packaging and
   deployment.
6. **Package the tested revision:** Build an archive from the tested source,
   excluding `.git`, virtual environments, and Python cache files.
7. **Pass the artifact:** Upload/download the archive between test and deploy
   jobs so the server receives the code that actually passed tests.
8. **Connect securely:** Read host, username, SSH private key, and a
   pre-verified `known_hosts` entry from GitHub Actions secrets. Do not disable
   host verification. Store them as repository or environment secrets and
   protect the `production` environment with required approvals.
9. **Copy the artifact:** Use `scp` to copy the artifact to the server and SSH
   to invoke the deployment helper.
10. **Install release dependencies:** The server-side helper creates a
    release-specific virtual environment and installs its dependencies.
11. **Run migrations:** Run a reviewed migration command before activating the
    new release. In this sample app no schema exists. Once one is added, include
    an executable `deploy/migrate.sh` that runs the actual migration command
    and exits nonzero on failure. Back up first for risky/destructive changes.
12. **Activate and restart:** Atomically move the `current` symlink to the new
    release and restart the `platform` service.
13. **Verify health:** Request `/health` locally through the Gunicorn socket
    and, in a full deployment, verify the HTTPS URL or ALB target health.
14. **Fail and roll back safely:** If restart or health check fails, restore
    the previous symlink, restart the old release, and exit nonzero so the
    Actions run is marked failed. Do not attempt an unplanned database rollback.

### GitHub secrets and server configuration required

Configure these example GitHub secrets for the `production` environment:

- `DEPLOY_HOST`: deployment server hostname or IP.
- `DEPLOY_USER`: dedicated non-root deployment login.
- `DEPLOY_SSH_KEY`: private SSH key for that login.
- `DEPLOY_KNOWN_HOSTS`: server host key gathered and verified out-of-band.

On the server, install the deployment helper at
`/usr/local/sbin/deploy-platform`, create its required release directories and
service account, and narrowly configure `sudo` so the deploy login can invoke
only that helper. Apply network rules limiting SSH to the CI runner/network
design in use; for managed runners, consider a controlled self-hosted runner or
AWS-native deployment approach if fixed IP allowlisting is required.

### Current sample limitation

The sample code currently has no models or migration history. The deployment
helper supports a migration hook, but no migration is executed in this
schema-less sample. In a real Django project, the hook could run
`python manage.py migrate --noinput`; in Flask, use the migration tool selected
by the project. Use the actual application's migration system—do not copy a
placeholder command into production.

## Part 3: Production incident investigation — `502 Bad Gateway`

### Investigation goal

A 502 from Nginx usually indicates that Nginx could not connect to its upstream
or received an invalid response. Investigate the service, upstream address,
socket/port, permissions, resources, and recent deployment before changing
state. Preserve useful logs first.

### Step 1: Check Nginx configuration and service

```bash
sudo nginx -t
sudo systemctl status nginx --no-pager
sudo nginx -T
sudo journalctl -u nginx --since "30 minutes ago" --no-pager
```

`nginx -t` checks syntax. `nginx -T` displays the effective configuration.
Confirm the configured `proxy_pass` points to the socket or port where Gunicorn
actually listens. Reload Nginx only after the config test passes.

### Step 2: Review Nginx logs

```bash
sudo tail -n 100 /var/log/nginx/error.log
sudo tail -n 100 /var/log/nginx/access.log
```

The error log often identifies the failure:

- `Connection refused`: no process is listening at the configured TCP port.
- `No such file or directory`: the Unix socket path is missing or mismatched.
- `Permission denied`: Nginx cannot traverse the socket directory or connect
  to the socket.
- `upstream timed out`: the app may be hung, overloaded, or waiting on a
  dependency.

### Step 3: Check application service and logs

```bash
sudo systemctl status platform --no-pager
sudo journalctl -u platform --since "30 minutes ago" --no-pager
sudo journalctl -u platform -n 200 --no-pager
```

Look for import errors, missing dependencies, missing environment variables,
failed migration/startup commands, repeated restarts, worker timeouts, or
database connection failures. Check which release `current` resolves to and
correlate timestamps with the deployment.

### Step 4: Check process, socket, port, and direct health

For the Unix socket used by this sample:

```bash
pgrep -a gunicorn
ps -ef | grep '[g]unicorn'
sudo ss -lxnp | grep gunicorn
sudo ls -ld /run/platform
sudo ls -l /run/platform/gunicorn.sock
curl --fail --silent --show-error \
  --unix-socket /run/platform/gunicorn.sock \
  -H 'Host: platform.example.com' \
  http://localhost/health
```

If Nginx is configured for a TCP upstream instead, verify the port and test the
matching local endpoint:

```bash
sudo ss -ltnp
curl -v http://127.0.0.1:<application-port>/health
```

Use the exact socket path or port from the active configuration; do not assume
it is the example value.

### Step 5: Check permission and host resource issues

```bash
ps -eo user,group,comm | grep nginx
namei -l /run/platform/gunicorn.sock
df -h
free -h
sudo journalctl -k --since "30 minutes ago" --no-pager
```

Nginx's worker account must be able to traverse parent directories and access
the Gunicorn socket. Check that the socket owner/group/mode is consistent with
the Nginx worker group. Check disk capacity, memory pressure, and kernel logs
for out-of-memory kills or other host failures.

### Common root causes

- Gunicorn stopped, crashed, or loops during startup.
- Nginx points at the wrong socket or TCP port.
- Socket directory or socket permissions prevent Nginx from connecting.
- A new release has missing dependencies, invalid configuration, or import
  errors.
- The deployment or system restart did not recreate the socket.
- Workers are blocked, timing out, or exhausted.
- CPU, memory, disk, or file descriptors are exhausted.
- A database or another dependency is unavailable and app requests hang.

### Safe restoration

1. Record the time, affected endpoints, deployment version, and relevant Nginx
   and application logs.
2. Check whether the 502 is isolated or affects all routes/instances.
3. Correct a confirmed configuration issue; run `nginx -t` before reloading.
4. If the issue correlates to the new release, switch to the last known-good
   release and restart the service.
5. Verify local `/health`, the Nginx route, the ALB target state, and the
   external HTTPS route.
6. Monitor logs and metrics after recovery; preserve the failed release for
   investigation.
7. Handle database schema recovery separately. Do not blindly reverse a
   migration or restore a database without assessing data loss and approval.

Avoid repeated restarts before gathering evidence: restarts may erase useful
state and will not fix a bad upstream path or permission configuration.

## Part 4: Security review

At least five risks and remediations are required. The following controls cover
the application, host, deployment path, AWS network, and recovery operations.

| Risk | Impact | Mitigation |
|---|---|---|
| Secrets stored in source or logs | Credential exposure can enable data access or account compromise. | Use Secrets Manager/Parameter Store or a root-owned restricted env file; never package populated secrets; rotate exposed values and prevent secret logging. |
| Overly permissive IAM | A compromised instance or pipeline can affect unrelated AWS resources. | Use EC2 instance roles and deployment identities scoped to required actions/resources. Avoid administrator policies and long-lived access keys. |
| Publicly exposed PostgreSQL | Attackers can directly probe or attempt to access the database. | Disable public accessibility; put RDS in private DB subnets; allow TCP 5432 only from the application security group; use strong auth and TLS. |
| Open security group ports | Exposed services create unnecessary attack surface. | Allow HTTPS (443) at the ALB; allow the app port only from the ALB security group; allow DB only from EC2; use Session Manager or restrict SSH to approved sources. |
| Missing TLS or weak certificate handling | Credentials and session data may be intercepted or traffic tampered with. | Use ACM on the ALB or an approved host certificate; redirect HTTP to HTTPS; renew automatically; restrict direct instance access. |
| Debug mode enabled or weak app settings | Stack traces or unsafe framework behavior can expose internals. | Disable debug in production, validate hostnames, use secure cookie settings, and configure proxy awareness only for trusted proxies. |
| Outdated/vulnerable dependencies | Known flaws may be exploitable through the application. | Pin/lock dependencies, scan in CI, update on a schedule, test fixes, and remove unused packages. |
| Weak Linux file/service permissions | Other local processes/users may read secrets or alter application files. | Run under a dedicated non-root account; restrict env files; keep releases read-only to runtime where practical; scope socket and log access. |
| Missing or untested backups | Accidental deletion, corruption, or failed changes may cause unrecoverable data loss. | Enable encrypted RDS automated backups and retention; protect S3 objects with suitable versioning/retention; regularly test restore procedures. |
| Missing monitoring and alerts | Outages, saturation, or security events may go unnoticed. | Monitor ALB 5xx/latency/targets, EC2 resources, RDS storage/connections, and app logs; alert an on-call channel with documented response actions. |
| Unsafe deployment access | An attacker or mistaken workflow can deploy arbitrary code. | Require production environment approval, restrict branch/workflow permissions, verify SSH host keys, use a dedicated identity, and narrowly limit server `sudo`. |
| Lack of audit logs | Investigations may not establish who changed or accessed resources. | Enable CloudTrail, centralize and protect system/application logs, retain them for a defined period, and restrict log deletion permissions. |

Review access and alerts periodically. Security controls should be tested, not
only documented.

## Part 5: Small code change — health-check endpoint

### Expected behavior

`GET /health` returns a small JSON response and HTTP 200 when the application
process is serving requests. An optional database probe runs `SELECT 1`. When
the database check is enabled and PostgreSQL is unavailable or configuration
is missing, the endpoint responds with HTTP 503 and a non-sensitive error
message.

### Implementation

The complete implementation is in [`app.py`](app.py). Its behavior is:

```python
@app.get("/health")
def health():
    check_database = os.getenv("HEALTHCHECK_DATABASE", "false").lower() in {
        "1", "true", "yes",
    }
    if not check_database:
        return jsonify(status="ok"), 200

    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return jsonify(status="error", database="unavailable"), 503

    try:
        with psycopg.connect(database_url, connect_timeout=2) as connection:
            connection.execute("SELECT 1")
    except psycopg.Error:
        return jsonify(status="error", database="unavailable"), 503

    return jsonify(status="ok", database="ok"), 200
```

The actual file also contains imports and Flask app setup. `connect_timeout=2`
limits the time spent waiting on an unavailable database, and the endpoint
does not reveal connection strings or exception details.

### How to run and test locally

Install dependencies and run the test suite:

```bash
python -m pip install -r requirements.txt
python -m unittest -v
```

Run locally for a manual smoke test:

```bash
flask --app app run --host 127.0.0.1 --port 5000
curl -i http://127.0.0.1:5000/health
```

Expected default response:

```http
HTTP/1.1 200 OK
Content-Type: application/json

{"status":"ok"}
```

With `HEALTHCHECK_DATABASE=true`, set `DATABASE_URL` through a local secret
source and test against a real development PostgreSQL instance. If unavailable,
the endpoint should return 503. The automated tests in
[`test_app.py`](test_app.py) mock Psycopg to test both healthy and failing
database paths without a live database.

For an ALB liveness probe, keep the database probe disabled: a database outage
should not cause the ALB to mark every otherwise-running application instance
unhealthy. Monitor database readiness and application dependency health
separately.

## Part 6: AWS architecture

### Architecture diagram

```text
                              Users
                                |
                         Route 53 DNS alias
                                |
                   HTTPS listener (ACM cert)
                                |
             Application Load Balancer (public subnets)
                     across 2+ Availability Zones
                                |
              EC2 Auto Scaling Group (private subnets)
                Gunicorn + Flask + Nginx/app tier
                     |                    |
                     |                    +------ S3
                     |                           uploads/assets
                     |
             RDS PostgreSQL (private DB subnets)
             Multi-AZ, encrypted, automated backups

   ALB + EC2 + RDS metrics/logs --> CloudWatch --> Alarms/notifications
   EC2 instance role -----------> Secrets Manager / Parameter Store / S3
```

### AWS services and operating model

- **EC2:** Run the application on Linux instances in private subnets across at
  least two Availability Zones. Use an Auto Scaling Group to replace unhealthy
  instances and scale to measured demand. Deploy versioned releases and apply
  OS patches on a controlled schedule. An ALB health check uses `/health`.
- **RDS PostgreSQL:** Use private DB subnets and set public accessibility off.
  Enable Multi-AZ for availability, encryption at rest, TLS for connections,
  automated backups, point-in-time recovery, and suitable maintenance windows.
  Allow connections only from the app-tier security group. Monitor connections,
  storage, CPU, and failover events.
- **Application Load Balancer:** Place it in public subnets across at least two
  zones. Terminate HTTPS using ACM, redirect HTTP to HTTPS, and forward only to
  healthy targets. Configure target protocol/port and `/health` accurately.
- **IAM:** Attach an instance profile to EC2. Grant only the required access
  (for example, read one named secret and access a specific S3 bucket/prefix).
  Use separate, scoped CI/CD permissions. Avoid static AWS credentials on
  hosts or in GitHub variables.
- **Security Groups:** ALB ingress allows HTTPS from expected client ranges
  (public internet if the service is public-facing). EC2 ingress allows the
  application port only from the ALB security group. RDS ingress allows TCP
  5432 only from the EC2 application security group. Admin access uses Session
  Manager or a constrained source, never broad open ingress.
- **S3:** Store uploads, generated artifacts, or static assets that must
  survive instance replacement. Block public access by default, enable
  encryption, grant least-privilege access to the application role, and apply
  versioning/lifecycle/retention policies appropriate to recovery and cost.
- **CloudWatch:** Collect ALB, EC2, and RDS metrics. Install/configure an agent
  if application or system logs must be shipped. Use log retention, encryption,
  and access controls.
- **Route 53:** Create an alias record for the application domain targeting
  the ALB. Keep DNS and certificate validation records controlled and audited.
- **Backups:** Enable and monitor RDS automated backups with an appropriate
  retention period; take snapshots before risky schema changes. Define a
  separate S3 recovery policy. Regularly perform restore exercises and record
  recovery time and recovery point results.
- **Monitoring and alerts:** Alarm on unhealthy ALB targets, elevated 5xx
  responses, latency, EC2 CPU/memory/disk pressure, RDS storage/connections,
  and failed backup jobs. Notify the responsible operations/on-call channel
  and document runbooks and escalation.

### Network flow summary

1. DNS resolves the application name through Route 53 to the ALB.
2. The ALB accepts HTTPS and presents its ACM certificate.
3. The ALB forwards requests to healthy application instances.
4. Instances access RDS privately and access S3/secrets through narrowly scoped
   IAM permissions.
5. CloudWatch receives operational metrics/logs and sends alarms to responders.

## Assignment delivery checklist

- [x] Written answers for Parts 1–6 are in this README.
- [x] Example GitHub Actions workflow is in
  [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml).
- [x] Health endpoint and tests are in [`app.py`](app.py) and
  [`test_app.py`](test_app.py).
- [x] Gunicorn/systemd and Nginx examples are in
  [`deploy/platform.service`](deploy/platform.service) and
  [`deploy/platform.nginx`](deploy/platform.nginx).
- [x] Sample deployment/rollback helper is in
  [`deploy/deploy-platform`](deploy/deploy-platform).
- [x] AWS architecture diagram and component descriptions are included above.
- [x] No real credentials or confidential organization data are included.
- [ ] Deploy and live-test AWS infrastructure after account access is restored.

The sample health endpoint has been unit tested. AWS provisioning and live
deployment remain unverified because the AWS account is suspended.
