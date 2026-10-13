# game-data

This script reads and transforms the mlp game data to be used for [All The Ponies](https://all-the-ponies.com/). The assets can be uploaded to https://assets.all-the-ponies.com/, which is a Cloudflare R2 bucket. Uploading and remote version checks require an S3-like bucket. Synthetic tests and local builds with an explicit game version and existing inputs do not.

# Setup

If you want to run this yourself, you need a few things.

Use Python 3.14 or newer and uv. Building the locked dependencies requires native
development tools, including a C compiler, pkg-config, Cairo, GObject Introspection,
Pango and protoc. The [Dockerfile](Dockerfile) lists the Linux system dependencies;
its x86-64 paths/downloads are not an Apple Silicon installation recipe. Install
the corresponding native libraries for your platform, then use the existing lock:

```shell
uv sync --locked
uv run --no-sync game-data --help
uv run --no-sync game-data build --help
```

Keep the default dependency groups: the current CLI imports `python-dotenv`,
which is in the dev group. Full image generation also needs Java, FFDec and FFmpeg;
the synthetic 3D tests below do not run that image stage.

For uploads, configure an S3-like bucket in `.env.dev` (the default/dev mode),
`.env.prod` (with `--env prod`), or exported environment variables:

```shell
S3_ENDPOINT=http://localhost:3000
S3_ACCESS_KEY=testing
S3_SECRET_KEY=testing
S3_REGION=us-east-1
S3_BUCKET=all-the-ponies-assets
```

To use notifications, use `notifications-example.json` to create the notifications config. Place it in either `notifications.dev.json` for running it in the dev environment, or `notifications.json` for production.

You need to get [ffdec](https://github.com/jindrapetrik/jpexs-decompiler/), and either add it to the path, or add `--ffdec ffdec.jar` to the command.

You also need to first install the `cairo` headers. Instructions can be found here: https://pycairo.readthedocs.io/en/latest/getting_started.html

To run the full pipeline and upload its output:

```shell
uv run game-data build --version latest --upload
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
PRIVATE_S3_SECRET_KEY=testing
PRIVATE_S3_REGION=us-east-1
PRIVATE_S3_BUCKET=private-bucket
```

The s3 bucket is for when you're running this on a system that doesn't have persistent storage.

## Optional 3D resources

The normal build includes native model/animation generation after Transformer.
See [source associations, scope, safe tests and publication](docs/3d-resource-associations.md).
Use `--skip model_gen` to skip this stage; generated resources share the existing
dist directory, sync process and root manifest.
