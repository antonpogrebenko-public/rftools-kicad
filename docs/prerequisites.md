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

## Target solve (task 2A.4), checked 2026-09-25

The frontend's `/docs/api` section, changelog entry and `/docs/python` went live first. The backend `0c02e01` followed. A free key was issued through the device grant (`client_id` `release-check`).

- **Without a key,** `POST /api/py/v1/calculate/solve` answers 401 with the key link.
- **G1 from `golden/kicad-golden.json`:** `microstrip-impedance`, solving `traceWidth` for `impedance` 50 on a 0.001 mm grid, with `substrateHeight` 1.51, `dielectricConstant` 4.5 and `copperThickness` 35.
  - **Result:** 200 with `value` 2.784 (the golden grid width), `unrounded` 2.784010722886359, `reached` true and `evaluations` 68.
  - **Forward result:** impedance 50.0001127254694, equal to the golden web output.
  - **Provenance:** nine members, `version` `api@0c02e01f0c4d`, `validRange` `inside`, `inputs.traceWidth` 2.784.
  - **Usage headers:** `X-Usage-Used` rose from 1 to 2.
- **A refusal** (`solveFor` set to the output `impedance`) answered 400 naming `solveFor`, and usage stayed at 2.

Afterwards the key was deleted from the machine that ran the check, and the account owner was asked to revoke it.
