# SMS Voice Gateway

A FastAPI-based SMS-to-voice gateway that receives incoming SMS webhooks, converts the message into text-to-speech, and places an outbound call through Asterisk/AMI to read the message aloud.

## Features

- SMS webhook endpoints for:
  - Twilio (`POST /sms/twilio`)
  - Vonage/Nexmo (`POST /sms/vonage`)
  - Generic JSON (`POST /sms/generic`)
- SMPP listener support for inbound gateway connections on configurable port `7070`
- Health and debug endpoints
- Admin JSON endpoints for configuration snapshots and delivery reports
- TTS provider support for:
  - Google Cloud Text-to-Speech
  - AWS Polly
  - OpenAI TTS
  - ElevenLabs
- Redis-backed cache and rate limiting
- Asterisk AMI integration for outbound call origination
- Dockerized deployment support

## Configuration model

This project uses an admin-first configuration model:

- `.env` is only for a small bootstrap set of settings needed to start the app.
- Operational settings are managed in the web admin UI and persisted to disk.
- Runtime requests load the saved configuration from the config store rather than directly from environment variables.

The persisted config store is used by the admin portal and by the application at runtime after startup.

### Bootstrap settings kept in `.env`

Keep only the settings needed to boot and reach the admin UI, such as:

- `HOST`
- `PORT`
- `DEBUG`
- `WEBHOOK_SECRET`
- `REDIS_URL`
- `REDIS_PREFIX`
- `AUDIO_CACHE_DIR`
- `ASTERISK_SOUNDS_DIR`

Depending on your deployment, you may also keep any other purely bootstrap/runtime-path setting required before the admin config is available.

### Admin-managed settings

These are edited in the web admin and saved to the config store:

- TTS provider and voice settings
- Google / AWS / OpenAI / ElevenLabs credentials and model settings
- AMI connection details
- SMPP settings
- SIP / outbound call settings
- rate limiting and playback settings
- phone formatting rules
- other operational gateway behavior

## First run

1. Create your minimal `.env` file from `.env.example`.
2. Start Redis and Asterisk so the gateway can reach them.
3. Run the application.
4. Open the admin UI in your browser.
5. Set the admin credentials if prompted, then save your gateway configuration in the Configuration page.
6. Save the configuration to persist it to disk.

After the first save, the application will load the persisted settings from the config store.

### Default admin access

The admin UI is protected by the application’s admin credentials. On first run, use the bootstrap admin credentials defined by your deployment or the application defaults, then change them immediately after login.

## Prerequisites

- Python 3.11+
- Redis
- Asterisk with AMI enabled
- A SIP trunk / channel configured in Asterisk
- One TTS provider credential set:
  - Google service account JSON path, or
  - AWS access keys, or
  - OpenAI API key, or
  - ElevenLabs API key
- Optional SMPP client credentials for inbound gateway bind/login

## Environment setup

Start from `.env.example` and create your local `.env` with only the bootstrap values you need to launch the app.

Example minimal `.env`:

```env
HOST=0.0.0.0
PORT=8000
DEBUG=false
WEBHOOK_SECRET=change_me_in_production
REDIS_URL=redis://localhost:6379/0
REDIS_PREFIX=sms_gw:
AUDIO_CACHE_DIR=./audio_cache
ASTERISK_SOUNDS_DIR=/var/lib/asterisk/sounds/sms_otp
```

If your deployment requires a different bootstrap path or host binding, adjust only those values in `.env`. All telephony, TTS, SMPP, and rate-limit behavior should be configured in the admin UI.

## Installation

### Simplified setup

1. Install Python 3.11+.
2. Install and start Redis, or use Docker Compose.
3. Run the setup script:
   ```bash
   python scripts/dev.py --setup-only
   ```
4. Create your minimal `.env` file from `.env.example`.
5. Start the app and complete configuration in the admin UI.

To create the virtualenv, install dependencies, and start the app in one step:

```bash
python scripts/dev.py
```

## Running locally

The app is served by Uvicorn. The main application object is `app.main:app`.

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

If you prefer to use the provided helper script:

```bash
./run.sh
```

### Important local dependencies

For the service to work end-to-end, make sure these are reachable:

- Redis at `REDIS_URL`
- Asterisk AMI at `AMI_HOST:AMI_PORT`
- SMPP port reachable at `SMPP_HOST:SMPP_PORT` if enabled
- Asterisk can access the audio cache directory configured by `AUDIO_CACHE_DIR` and `ASTERISK_SOUNDS_DIR`

## Web admin configuration

Use the admin portal to manage operational settings.

Common tasks include:

- selecting the TTS provider
- entering provider credentials
- configuring AMI credentials
- enabling and configuring SMPP
- adjusting rate limits and playback behavior
- reviewing configuration snapshots and delivery reports

The admin configuration is persisted to disk by the application. This means settings survive restarts without needing to be re-entered in `.env`.

## Docker

A `Dockerfile` and `docker-compose.yml` are provided.

### Run with Docker Compose

1. Create your minimal `.env` file from `.env.example`.
2. Start the stack:
   ```bash
   docker compose up --build
   ```

This starts:

- `redis` on `127.0.0.1:6379`
- `gateway` on `http://localhost:8000`

The compose file sets `REDIS_URL=redis://redis:6379/0` for the gateway container, so the app talks to Redis by service name.

Useful commands:

```bash
docker compose logs -f gateway
docker compose ps
docker compose down
```

### Standalone Docker image

If you already have Redis and Asterisk available outside Docker, you can run only the gateway container:

```bash
docker build -t sms-voice-gateway .
docker run --rm -p 8000:8000 --env-file .env sms-voice-gateway
```

## systemd deployment on Linux

For a non-Docker Linux deployment, you can install the gateway as a `systemd` service.

Files added for this flow:

- `deploy/sms-voice-gateway.service` – service unit template
- `deploy/install_systemd_service.sh` – installs, enables, and restarts the service
- `deploy/manage_systemd_service.sh` – helper wrapper for `systemctl` / `journalctl`

### Example install

```bash
chmod +x deploy/install_systemd_service.sh deploy/manage_systemd_service.sh
SERVICE_USER=smsgateway SERVICE_GROUP=smsgateway PYTHON_BIN=/opt/sms-voice-gateway/.venv/bin/python WORKING_DIRECTORY=/opt/sms-voice-gateway ENV_FILE=/opt/sms-voice-gateway/.env ./deploy/install_systemd_service.sh
```

This will:

1. render the unit file from the template
2. copy it to `/etc/systemd/system/sms-voice-gateway.service`
3. reload `systemd`
4. enable the service on boot
5. restart the gateway service
6. show the current service status

### Routine service management

```bash
./deploy/manage_systemd_service.sh status
./deploy/manage_systemd_service.sh restart
./deploy/manage_systemd_service.sh logs
```

### Admin health restart integration

The Health page restart control now supports Linux `systemctl` restarts when the app is running on Linux with `systemctl` available. By default it targets:

```text
sms-voice-gateway.service
```

Override the service name for the admin health restart button with:

```bash
export SMS_GATEWAY_SYSTEMD_SERVICE=my-custom-gateway.service
```

The web admin will continue to use Docker restart actions when running in a Docker Compose-managed environment.

## API endpoints

- `GET /health` - service health check
- `GET /cache/stats` - audio cache statistics
- `POST /cache/evict` - evict expired cached audio
- `POST /debug/call` - manually trigger a test call
- `GET /admin/config` - admin-friendly configuration snapshot
- `GET /admin/reports` - delivery report summary and recent records
- `GET /admin/reports/{report_id}` - fetch a specific report by AMI action ID or timestamp
- `POST /sms/twilio` - Twilio webhook
- `POST /sms/vonage` - Vonage/Nexmo webhook
- `POST /sms/generic` - generic SMS webhook

## SMPP support

This project includes a lightweight SMPP listener intended for inbound bind/login verification and future gateway integration.

- Default listen port: `7070`
- Configurable via the web admin:
  - `SMPP_ENABLED`
  - `SMPP_HOST`
  - `SMPP_PORT`
  - `SMPP_USERNAME`
  - `SMPP_PASSWORD`

Notes:

- The listener is started when the app boots if `SMPP_ENABLED=true`.
- The implementation currently supports basic SMPP bind/unbind handling with credential validation.
- Logs show connection lifecycle events without exposing secrets.

## Troubleshooting

- **Health check returns `degraded`**: verify Redis and Asterisk AMI are reachable.
- **Twilio webhook returns 403**: ensure `WEBHOOK_SECRET` is configured correctly and Twilio is sending the expected signature header.
- **No outbound call is placed**: confirm AMI credentials, SIP channel prefix, and the Asterisk dialplan context are correct.
- **Audio files are not found by Asterisk**: make sure `AUDIO_CACHE_DIR` and `ASTERISK_SOUNDS_DIR` point to a shared or mounted path.
- **Wrong TTS voice or language**: verify the selected provider-specific voice settings in the admin UI.
- **Redis connection errors in Docker**: use the compose-provided `REDIS_URL=redis://redis:6379/0` or point to a reachable Redis instance.
- **SMPP bind fails**: confirm `SMPP_USERNAME` and `SMPP_PASSWORD` match the connecting gateway credentials, and that the port is exposed if running in Docker.

## Notes

- The application prefers persisted admin configuration for operational settings and uses `.env` only for bootstrap values.
- Twilio request validation is minimal in code; for production use, consider the official Twilio validator library.
