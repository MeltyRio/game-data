# game-data

This script reads and transforms the mlp game data to be used for [All The Ponies](https://all-the-ponies.com/). The assets are uploaded to https://assets.all-the-ponies.com/, which is a Cloudflare R2 bucket. This script is not meant to be run without an s3-like bucket, and you do need some expertise to run anything (the below is mostly just for me to remember commands).

# Setup

If you want to run this yourself, you need a few things.

In a `.env` file, or just setting these environment variables, you can configure the S3-like bucket. Note: the script won't work without one.

```shell
S3_ENDPOINT=http://localhost:3000
S3_ACCESS_KEY=testing
S3_SECRET_KEY=testing123
S3_REGION=us-east-1
S3_BUCKET=all-the-ponies-assets
```

To run a local S3 server, use the included `compose.yaml`, which starts [Silo](https://silo.pgsty.com/), a local s3 server.

```shell
docker compose up -d
```

To stop the local S3 server:

```shell
docker compose down
```

To delete the local S3 server data

```shell
docker compose down -v
```

The S3 API is available at http://localhost:3000 and the console at http://localhost:3001. Data is stored in the `atp-silo-data` volume; `docker compose down -v` removes it.

The `silo-init` service creates the `all-the-ponies-assets` bucket and grants anonymous read-only access to it, so uploaded objects can be fetched without credentials, for example http://localhost:3000/all-the-ponies-assets/game_version.json. Set `S3_BUCKET` to use a different bucket name, or `SILO_PUBLIC_ACCESS=public` to allow anonymous writes as well.

To use notifications, use `notifications-example.json` to create the notifications config. Place it in either `notifications.dev.json` for running it in the dev environment, or `notifications.json` for production.

You need to get [ffdec](https://github.com/jindrapetrik/jpexs-decompiler/), and either add it to the path, or add `--ffdec ffdec.jar` to the command.

You also need to first install the `cairo` headers. Instructions can be found here: https://pycairo.readthedocs.io/en/latest/getting_started.html

And of course to actually run it

```shell
uv run game-data --version latest --upload
```

If all of this is too complicated, you can just use the docker container.

```shell
docker build -t game-data . 
docker run --env-file .env game-data
```


## Google Play API

This project uses the google play api to find updates as soon as possible, but that does require a google account which I do not provide. You are able to run the script without one though, as it also checks the google play website, it just may not find an update immediately.

To set this up, you need to have a token dispenser. See https://gitlab.com/AuroraOSS/aurora-dispenser to self-host.

Set this environment variable

```shell
PLAYSTORE_DISPENSER_URL="https://example.com/api/auth" # REPLACE WITH YOUR URL
```

The login config will be saved to `.playstoreapi`. You may also upload this to an s3 bucket if you want, but since this is sensitive, make a private s3 bucket, and set these environment variables.

```shell
PRIVATE_S3_ENDPOINT=http://localhost:3000
PRIVATE_S3_ACCESS_KEY=testing
PRIVATE_S3_SECRET_KEY=testing123
PRIVATE_S3_REGION=us-east-1
PRIVATE_S3_BUCKET=private-bucket
```

The s3 bucket is for when you're running this on a system that doesn't have persistent storage.
