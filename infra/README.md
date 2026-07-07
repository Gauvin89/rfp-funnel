# Backend on AWS — deployed from this GitHub repo

Shared per-card notes / touch log for the RFP board, hosted in **Perigon's AWS**,
but **controlled entirely from this repo**. Push code → GitHub Actions deploys it.
Want it gone? Run the **Destroy** workflow — one click, everything in AWS disappears,
your code stays here.

```
GitHub repo (you own the code)
      │  push to main
      ▼
GitHub Actions ──OIDC (no stored keys)──▶ AWS CloudFormation
      │                                        ├─ Lambda  (rfp-board-api, Function URL)
      │                                        └─ DynamoDB (rfp-board-activity, encrypted)
      ▼
Encrypted board on GitHub Pages ──HTTPS + bearer token──▶ Lambda Function URL
```

## Pieces
| File | What it is |
|---|---|
| `template.yaml` | SAM: Lambda + DynamoDB + Function URL (the app) |
| `app/handler.py` | the API — `GET /activity`, `POST /activity/{cardId}`, `GET /health` |
| `bootstrap.yaml` | one-time: GitHub OIDC provider + a deploy role scoped to this repo |
| `../.github/workflows/deploy-backend.yml` | push → `sam deploy` |
| `../.github/workflows/destroy-backend.yml` | manual → delete the stack (**pull the plug**) |

No secrets live in the repo. The API token is a GitHub secret injected at deploy;
the frontend copy lives only in the gitignored `config/.env` → the encrypted board.

## One-time setup (a Perigon AWS admin does this once)
1. **Bootstrap** the OIDC provider + deploy role (needs IAM admin):
   ```bash
   aws cloudformation deploy \
     --template-file infra/bootstrap.yaml \
     --stack-name rfp-board-bootstrap \
     --capabilities CAPABILITY_NAMED_IAM
   # If the account already has a GitHub OIDC provider, add:
   #   --parameter-overrides CreateOIDCProvider=no
   ```
2. **Grab the role ARN:**
   ```bash
   aws cloudformation describe-stacks --stack-name rfp-board-bootstrap \
     --query "Stacks[0].Outputs[?OutputKey=='RoleArn'].OutputValue" --output text
   ```
3. **Add it as a GitHub secret** (or hand the ARN to Claude to set it):
   ```bash
   gh secret set AWS_DEPLOY_ROLE_ARN --repo Gauvin89/rfp-funnel --body "<RoleArn>"
   # If Perigon's AWS isn't us-east-1, also:  gh variable set AWS_REGION --body "<region>"
   ```
   `BOARD_API_TOKEN` is already set as a repo secret.

## Deploy
Push any change under `infra/**`, or run **Actions → Deploy backend → Run workflow**.
The run summary prints:
```
API_URL=https://xxxx.lambda-url.<region>.on.aws/
```

## Turn the board's shared notes on
```bash
echo 'API_URL="https://xxxx.lambda-url.<region>.on.aws/"' >> config/.env   # (Claude does this)
python3 response/build_dashboard.py
python3 response/encrypt_board.py
git add index.html && git commit -m "enable shared notes" && git push
```

## Pull the plug
**Actions → Destroy backend → Run workflow → type `DELETE`.** The Lambda + DynamoDB
(and the notes in it) are deleted; the board silently falls back to per-browser notes.
To fully de-provision, also delete `rfp-board-bootstrap` and revoke nothing else —
there are no long-lived keys.

## Cost
DynamoDB on-demand + Lambda + Function URL for a handful of users = **cents/month**,
almost entirely within the AWS Free Tier.
