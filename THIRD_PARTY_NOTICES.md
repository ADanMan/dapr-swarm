# Third-party notices

The application code in this repository was developed independently for this project. It does not contain the implementation of BeeGate, Hermes, Beeclaw Station, or the Swarms framework.

## Bundled component

`app/static/vendor/markdown-it.min.js` is the markdown-it browser distribution. It is licensed under MIT by its upstream authors. The original copyright and license text is preserved in `app/static/vendor/markdown-it.LICENSE`. Project: https://github.com/markdown-it/markdown-it.

## External dependencies

Dapr Agents and Dapr provide workflow orchestration. FastAPI and other Python packages are installed separately from the pinned dependency manifests. Docker Compose references Dapr, Redis, Python, and SearXNG container images. Node development dependencies are declared in package-lock.json. These dependencies and images are not relicensed by this project's MIT license; their upstream licenses and notices continue to apply.

Codex CLI is an external prerequisite used for local command sandboxing. No Codex binary, account credential, or model access is included. Provider accounts, API endpoints, credentials, private task data, and local operational reports are not distributed.
