# Where did all my tokens go? The agentgateway demo

The live demo from *Where Did All My Tokens Go? Using agentgateway to Keep Your AI Usage Under Control* (AGNTCon + MCPCon North America 2026). The slides are [Where_Did_All_My_Tokens_Go_AGNTCon_MCPCon_NA_2026.pdf](Where_Did_All_My_Tokens_Go_AGNTCon_MCPCon_NA_2026.pdf). Everything runs locally against open-source [agentgateway](https://github.com/agentgateway/agentgateway), in about 15 minutes.

You'll install agentgateway, put two providers behind one endpoint, give every user and agent its own virtual key, see who spent what, then cap spend with a 5-hour and a weekly budget and limit which models a key can use.

Fast-forward is the time skip between virtual keys and analytics. It fills in a week of traffic so the cost chart has something to show.

## What you need

- macOS or Linux with `curl`, `python3`, and `openssl`
- An Anthropic API key and an OpenAI API key. No keys? See [Try it without API keys](#try-it-without-api-keys).
- The full run spends roughly $0.25 of API credit.
- Ports 4000, 15000, 15020, and 15021 free. `./scripts/preflight.sh` checks.

## 1. Open a demo shell

```sh
git clone https://github.com/sodman/agentgateway-agntcon-demo-2026
cd agentgateway-agntcon-demo-2026
cp .env.local.example .env.local      # then put your two API keys in it
source demo.env
./scripts/preflight.sh
```

`demo.env` keeps everything in `~/.agw-demo`: nothing is installed system-wide and no sudo is needed. Use two terminals, and run `source demo.env` in both.

Put both keys in `.env.local` before you start the gateway. It reads `$ANTHROPIC_API_KEY` at the beginning and `$OPENAI_API_KEY` when the second provider is added, and it only sees the environment of the terminal that launched it.

## 2. Install

```sh
curl -sL https://agentgateway.dev/install | bash
agentgateway
```

Leave it running. The log prints `serving UI at http://localhost:4000/ui`. Open that URL. Started with no arguments, agentgateway writes its own config and a SQLite database, which is what analytics and budgets read.

Use port 4000, the same port the gateway is serving. The admin listener on port 15000 is a different origin, and the chat playground will not reach the models from there.

## 3. Add models (UI)

One provider by hand. The fast-forward in step 5 adds OpenAI the same way, so you do not click both.

1. **LLM > Providers > Add provider**. Name it `anthropic`. Provider: **Anthropic**.
2. **Provider API key**: choose **Env var**, and type `ANTHROPIC_API_KEY`. The UI stores `$ANTHROPIC_API_KEY`. The gateway expands that from its environment, so the key never lands in the config file. Do not paste the key into the API key box.
3. **LLM > Models**. Add a model whose incoming name is `claude-sonnet-5`, on that Anthropic provider. If the form asks for an outgoing model, use `claude-sonnet-5` again.
4. **LLM > Chat Playground**. Say hi.

## 4. Virtual keys (UI)

**LLM > Virtual API Keys > New key**. Name `alice`. Under **Metadata**, add `user` = `alice` (the form has a `user` shortcut). **Save key**. You do not need to copy the key value.

In the second terminal:

```sh
./ask.sh alice claude-sonnet-5
```

`ask.sh` looks the key up by name. The same key is what you will use for the other provider in a minute.

## 5. Fast-forward a week

```sh
./fast-forward-a-week.sh
./ask.sh alice gpt-5-mini             # same key, second provider
```

The script adds OpenAI, the rest of the models, and keys for `bob`, `support-team`, `nightly-agent`, and `intern`, through the same API the UI uses. It turns on **Strict** validation, so every later request needs a key, then replays a week of traffic. The calls happen immediately, and the script then dates those log rows across the past seven days: nightly-agent around 02:40, everyone else during the daytime. It prints how many requests each key made. The dollar amounts show up on the Analytics page.

## 6. Cost analytics (UI)

**LLM > Analytics**. Set the measure to **Cost**, group by **User**, and set the range to **Last 7 days**. The picker opens on **Last 24 hours**, which shows the most recent day of the same traffic.

The request counts are close together. The spend is not.

## 7. Spend windows (UI)

**LLM > Virtual API Keys > nightly-agent**. Open **Budgets** and add two. The form starts each one at `30d` and **Audit only**. Change both, or the next command will not get a 429.

| Name | Limit amount | Limit unit | Rolling window | When limit is reached |
| --- | --- | --- | --- | --- |
| `5-hour` | `0.02` | USD | `5h` | **Block requests** |
| `weekly` | `1` | USD | `7d` | **Block requests** |

Use `7d` or `168h`. `1w` is rejected. **Save key**.

```sh
./ask.sh nightly-agent claude-sonnet-5 --until-blocked
```

Budgets count spend from the moment you save them, so the week's history does not apply. The request that crosses the cap still completes. The next one is `429` with `budget_exceeded`.

## 8. Model allow-lists (UI)

**LLM > Virtual API Keys > intern > Model access**. Set **Access mode** to **Selected models**, and add the pattern `*-mini`. **Save key**.

```sh
./ask.sh intern claude-sonnet-5       # 403 model_not_allowed
./ask.sh intern gpt-5-mini            # 200
```

A rejected request never reaches the provider.

Prefer the command line for steps 7 and 8? `./scripts/apply-policies.sh` writes both through the API.

## Try it without API keys

```sh
export DEMO_MOCK=1
source demo.env
python3 scripts/mock-llm.py           # in its own terminal: a stand-in for OpenAI and Anthropic
```

Then follow the same steps. After adding the provider in step 3, run `python3 scripts/agw.py point-at-mock` once so the gateway uses the stand-in. Costs come out in the same shape, because the mock reports realistic token counts and the gateway prices them from its built-in catalog. Mock mode substitutes stand-in credentials, so the keys in `.env.local` are not sent.

## Clean up

```sh
./scripts/reset.sh                    # stops the demo gateway and deletes ~/.agw-demo
```

`reset.sh` only stops an agentgateway whose binary is inside `~/.agw-demo`. If preflight says a port is taken, something else is listening.

## Docs

- Budgets and model access: https://agentgateway.dev/docs/standalone/latest/llm/cost-controls/budget-limits/
- Project: https://github.com/agentgateway/agentgateway · https://agentgateway.dev

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
