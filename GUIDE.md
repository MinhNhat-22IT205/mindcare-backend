# GUIDE

## Prerequisites
- Docker and Docker Compose

## Run with Docker
1. Build and start the app:
   ```bash
   docker compose up --build
   ```
2. Open http://localhost:8000

## Run migrations
In another terminal:
```bash
docker compose exec web python manage.py migrate
```

## Optional environment variables
- `DJANGO_SECRET_KEY`: custom Django secret key (dev default is provided in `docker-compose.yml`)
- `DJANGO_ALLOWED_HOSTS`: comma-separated hostnames (default: `localhost,127.0.0.1`)

## Stop services
```bash
docker compose down
```
