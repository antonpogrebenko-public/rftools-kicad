# Prerequisites

The plugin depends on rftools.io's metered API (openspec change `api-metering` in the rftools monorepo) and on the target-solve call (`kicad-plugin`, design Decision 10). This file records the checks that showed each one live.

## api-metering (task 1.1), checked 2026-09-25

- **`GET https://rftools.io/api/py/v1/usage` answers a key.** A free key was issued through the device grant (`/v1/oauth/device/code`, approved on `/device/`). The endpoint returned an allowance of 5 with 0 used, and reading it again did not change the count.
- **A calculator response carries `provenance`.** `POST /api/py/v1/calculate` for `vswr-return-loss` returned 200 with these headers:
  - `X-Usage-Allowance: 5`
  - `X-Usage-Used: 1`
  - `X-Usage-Reset: 2026-10-01T00:00:00+00:00`

  The body carried the nine-member provenance envelope: `version` `api@2cb8d8d17776`, the Pozar `formulaRef`, and a `validRange` of `inside`. A second call to `/usage` then read 1 used.
- **`pip install rftools-io==0.3.0` provides `Client.usage`.** Confirmed in a Python 3.12 environment: `rftools 0.3.0`, and `Client.usage` is callable.

Afterwards the key was deleted from the machine that ran these checks, and the account owner was asked to revoke it.

## Target solve (task 2A.4)

Not yet live. `POST /api/py/v1/calculate/solve` and `rftools-io` 0.4.0 (`Client.solve`) are built. They ship after the frontend's `/docs/api` section, and the result will be recorded here.
