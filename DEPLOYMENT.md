# Deploying The Three Phase Ampacity Calculator on Render

The Ampacity Calculator deploys as one Python web service. The Python process serves both the API and the static frontend, so the app does not need a separate frontend service, database, persistent disk, or secret.

## Before deploying

1. Push this repository, including `render.yaml`, to GitHub, GitLab, or Bitbucket.
2. Confirm the repository root contains `embr-server.py` and `render.yaml`. If the app is inside a larger monorepo, set the Blueprint service's `rootDir` to this folder.
3. Confirm the GitHub Actions **Test** workflow passes. It runs the backend input suite, both engineering verification scripts, and the frontend integration suite.

## Blueprint deployment (recommended)

1. Sign in to [Render](https://dashboard.render.com/).
2. Select **New > Blueprint**.
3. Connect this repository and choose the branch to deploy (normally `main`).
4. Review the detected `embr` web service and apply the Blueprint.
5. Wait for the build, Python verification suites, startup, and `/healthz` check to pass.
6. Open the assigned `https://<service-name>.onrender.com` URL.

The included `render.yaml`:

- uses Render's native Python runtime and Python 3.12;
- installs `requirements.txt`;
- runs every Python regression/engineering verification script during the build;
- starts `embr-server.py`, which listens on Render's injected `PORT` on all interfaces;
- uses `/healthz` for application health checks;
- deploys new commits from the linked branch automatically.

The free plan is selected for a simple first deployment. Free services may spin down while idle, so the first request after inactivity can be slower. Change `plan` in `render.yaml` or in Render if this is unsuitable for production use.

## Manual web-service setup

If you do not use the Blueprint, create a Python web service with these settings:

| Setting | Value |
|---|---|
| Runtime | Python 3 |
| Build command | `pip install -r requirements.txt && python test_input_validation.py && python tb880_verification.py && python mv_iec_crosscheck.py` |
| Start command | `python embr-server.py` |
| Health check path | `/healthz` |
| Python version | `3.12` (or a tested newer version) |

Do not set `PORT`; Render supplies it. The Ampacity Calculator has no required environment variables.

## Optional environment variables

| Variable | Purpose |
|---|---|
| `EMBR_ALLOWED_ORIGIN` | Allows one explicit cross-origin caller. Leave unset for the normal, safer same-origin deployment. |
| `PORT` | Local override only. Render injects this automatically. Default locally: `8080`. |

## Post-deploy smoke test

Replace the hostname below with the Render URL:

```bash
curl --fail https://embr.onrender.com/healthz
curl --fail https://embr.onrender.com/
```

Then verify in a browser:

1. The home page, logo, screenshots, and validation PDFs load.
2. MV, DC, and LVAC inputs calculate without an error banner.
3. Save/load configuration works in the browser.
4. PDF export downloads a valid report.
5. Soil-temperature lookup works. This browser-side feature calls Open-Meteo and requires the user's browser/network to reach that service.

## Security and access

A Render web service is public unless you add access controls. The Ampacity Calculator has no user accounts or authentication. If calculations or validation documents must remain private, put the service behind an approved identity-aware proxy or use Render access controls available to your plan before sharing the URL.

## Troubleshooting

- **Build fails in a Python suite:** treat this as a blocked release. Read the first failing assertion in the Render build log; the previous live deployment remains unchanged.
- **`502 Bad Gateway` or port scan timeout:** verify the start command is `python embr-server.py` and do not replace Render's `PORT` value.
- **`npm error Missing script: "start"`:** Render created the service with the Node runtime. The repository includes a compatibility `npm start` path so the service can recover, but the supported configuration is **Runtime: Python 3**, **Build Command:** the command above, and **Start Command:** `python embr-server.py`. Update those settings or recreate the service from `render.yaml`.
- **Health check fails:** open `/healthz`; it must return HTTP 200 and `{"status":"ok"}`.
- **PDF export fails:** confirm `reportlab` installed successfully from `requirements.txt` in the build log.
- **Static assets return 404:** the server pins assets to the directory containing `embr-server.py`, so verify all tracked assets were pushed.
- **Slow first request:** this is expected after an idle free-plan service spins up. Use a paid always-on instance if cold starts are unacceptable.
- **Open-Meteo lookup fails:** calculations still work. Check browser privacy extensions, outbound network policy, and Open-Meteo availability.

## Rollback

From the service's Render **Events** page, redeploy a previously successful commit. Temporarily disable automatic deploys if the latest branch commit would immediately reintroduce the faulty version.
