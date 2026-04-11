# SMS Voice Gateway

A FastAPI-based SMS-to-voice gateway that receives incoming SMS webhooks, converts the message into text-to-speech, and places an outbound call through Asterisk/AMI to read the message aloud.

## Features

- SMS webhook endpoints for:
  - Twilio (`POST /sms/twilio`)
  - Vonage/Nexmo (`POST /sms/vonage`)
  - Generic JSON (`POST /sms/generic`)
- Health and debug endpoints
- TTS provider support for:
  - Google Cloud Text-to-Speech
  - AWS Polly
  - OpenAI TTS
  - ElevenLabs
- Redis-backed cache and rate limiting
- Asterisk AMI integration for outbound call origination
- Dockerized deployment support

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

## Environment setup

The application reads configuration from `.env` using `pydantic-settings`. Start from `.env.example` and create your local `.env`.

Common settings include:

- `HOST`, `PORT`, `DEBUG`
- `WEBHOOK_SECRET`
- `TTS_PROVIDER`
- `TTS_LANGUAGE`, `TTS_VOICE`, `TTS_SPEAKING_RATE`, `TTS_AUDIO_ENCODING`
- `GOOGLE_CREDENTIALS_JSON`
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, `AWS_POLLY_VOICE_ID`, `AWS_POLLY_ENGINE`
- `OPENAI_API_KEY`, `OPENAI_TTS_MODEL`, `OPENAI_TTS_VOICE`
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`
- `AUDIO_CACHE_DIR`, `ASTERISK_SOUNDS_DIR`, `AUDIO_CACHE_TTL`
- `AMI_HOST`, `AMI_PORT`, `AMI_USERNAME`, `AMI_SECRET`
- `SIP_CHANNEL_PREFIX`, `OUTBOUND_CALLER_ID`
- `ASTERISK_CONTEXT`, `ASTERISK_EXTEN`, `ASTERISK_PRIORITY`
- `REDIS_URL`, `REDIS_PREFIX`
- `RATE_LIMIT_HOURLY`, `RATE_LIMIT_DAILY`
- `PHONE_REGEX`, `STRIP_CALL_PREFIX`
- `PLAYBACK_REPEATS`, `PLAYBACK_PAUSE_MS`

Example:

```env
TTS_PROVIDER=google
REDIS_URL=redis://localhost:6379/0
AMI_HOST=127.0.0.1
AMI_PORT=5038
AMI_USERNAME=manager
AMI_SECRET=manager_secret
WEBHOOK_SECRET=change-me
```

## Installation

### Windows

1. Install Python 3.11+.
2. Install and start Redis, or use Docker Compose.
3. Create and activate a virtual environment:
   ```powershell
   python -m venv .venv
   .venv\Scripts\Activate.ps1
   ```
4. Install dependencies:
   ```powershell
   pip install -r requirements.txt
   ```
5. Copy `.env.example` to `.env` and edit values.

### Linux/macOS

1. Install Python 3.11+.
2. Install and start Redis, or use Docker Compose.
3. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. Copy `.env.example` to `.env` and edit values.

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
- Asterisk can access the audio cache directory configured by `AUDIO_CACHE_DIR` and `ASTERISK_SOUNDS_DIR`

## Docker

A `Dockerfile` and `docker-compose.yml` are provided.

### Build and run with Docker Compose

```bash
docker compose up --build
```

This starts:

- `redis` on `127.0.0.1:6379`
- `gateway` on `http://localhost:8000`

The compose file sets `REDIS_URL=redis://redis:6379/0` for the gateway container.

### Standalone Docker image

```bash
docker build -t sms-voice-gateway .
docker run --rm -p 8000:8000 --env-file .env sms-voice-gateway
```

## API endpoints

- `GET /health` - service health check
- `GET /cache/stats` - audio cache statistics
- `POST /cache/evict` - evict expired cached audio
- `POST /debug/call` - manually trigger a test call
- `POST /sms/twilio` - Twilio webhook
- `POST /sms/vonage` - Vonage/Nexmo webhook
- `POST /sms/generic` - generic SMS webhook

## Troubleshooting

- **Health check returns `degraded`**: verify Redis and Asterisk AMI are reachable.
- **Twilio webhook returns 403**: ensure `WEBHOOK_SECRET` is configured correctly and Twilio is sending the expected signature header.
- **No outbound call is placed**: confirm `AMI_*` credentials, `SIP_CHANNEL_PREFIX`, and the Asterisk dialplan context are correct.
- **Audio files are not found by Asterisk**: make sure `AUDIO_CACHE_DIR` and `ASTERISK_SOUNDS_DIR` point to a shared or mounted path.
- **Wrong TTS voice or language**: verify the selected provider-specific voice settings.
- **Redis connection errors in Docker**: use the compose-provided `REDIS_URL=redis://redis:6379/0` or point to a reachable Redis instance.

## Notes

- The gateway uses `.env` by default and ignores unknown extra environment variables.
- Twilio request validation is minimal in code; for production use, consider the official Twilio validator library.
