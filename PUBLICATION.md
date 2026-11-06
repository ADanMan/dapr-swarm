# v0.4.1: first public source release

Published source is a reviewed snapshot of the independently developed local application. Public Git history starts here. The private development history, screenshots, operational transcripts, task identifiers, task artifacts, benchmark outputs, personal paths, credentials, and machine-specific endpoint are not included. The original local installation and its historical tags remain separate.

## Changes for public use

- Configure the API URL, exact GLM model ID, and API key explicitly through environment variables or an ignored `.env` file.
- No automatic credential reads from Hermes or another application.
- A nonfunctional `.env.example` and a small synthetic input/task example are included.
- Generate the SearXNG instance secret in memory unless the user supplies one.
- Exclude secrets and local dependencies from Git and Docker build context.
- Preserve the markdown-it MIT notice. License the original application code under MIT; external dependencies retain their licenses.
- Pin the included Hermes skill and installer to the public `v0.4.1` tag.

## Checks performed on 6 November 2026

All checks were sequential and offline except GitHub account/repository discovery and public documentation lookup. No running swarm, provider request, service rebuild, mutation testing, or load test was started.

- Reviewed all 226 reachable historical blobs for recognizable credential formats, personal paths, endpoint details, and secret assignments. No recognized credential token was found. Historic paths and the local gateway address were excluded by publishing a new allowlisted snapshot instead of the old history.
- Scanned the complete publication candidate for those patterns and reviewed URL domains. Remaining non-public IP literals are negative security-test fixtures; example domains and dummy credentials are synthetic fixtures.
- Five launch-configuration unit tests passed: missing values, unusable example, environment precedence, rejection of credentials in URLs, and search-secret generation/preservation.
- All 33 existing DOM/UI tests passed.
- Python syntax parsing passed for 27 files; JSON, TOML, YAML, JavaScript and shell syntax checks passed.
- `docker compose config --quiet` passed with dummy values. It did not start containers.
- `uv lock --check --offline` passed. The Hermes skill validator passed; embedded CLI bytes match `app/cli.py`.

The first syntax-check attempt used the system Python without `tomllib`; it was rerun successfully under the required Python 3.12 environment. Full backend tests, a clean dependency installation, a container build, public-provider compatibility, and live end-to-end execution were not rerun for publication. Historical local acceptance is not presented as fresh validation of this public snapshot.

The application is for a trusted single user on localhost. It is not a hosted multi-user service and does not provide authentication for public exposure. A pattern scan cannot guarantee absence of every possible secret; source selection, new history, and exclusion of local artifacts are additional controls.
