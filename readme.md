# Governments on Superintelligence
What do government officials say about AGI, its risks, and policy approaches to it? We collect quotes with metadata from official source into a database, and analyze. 

This repository covers quote collection, filtering, and judging. You might be looking for the [accompanying repository](https://github.com/lennart-finke/governments-on-superintelligence) with a website displaying the data. For an explanation of the method, see [here](https://github.com/lennart-finke/governments-on-superintelligence/blob/main/method.md).

## Replication
For a complete replication, install with 

```bash
uv sync --extra dev
cp .env.example .env
```

and step through using the command line interface:

1. Fetching from government websites and some inofficial mirrors with `tracker fetch`. This can take quite a while, up to a few days. For the US sources, you'll need to set `GOVINFO_API_KEY` in `.env`, same for a Russian source with `DUMA_API_TOKEN` and `DUMA_APP_TOKEN`.
2. Parse documents into utterances with `tracker parse`.
3. Filter utterances into potentially AI-relevant utterances using a keyword list with `tracker filter`.
4. Filter candidate utterances into AGI / AI Risk / AI Regulation-relevant quotes using LLM-as-a-judge, with `tracker adjudicate  --judge glm`. You'll need to set `OPENROUTER_API_KEY` in `.env`. Spend API credits. 
5. Filter canditate utterances further with a confirmation judge, with `tracker promote`. Spends API credits.
6. Annotate finalized utterances with `tracker refine`. Spends API credits.
7. Link duplicate quotes and speakers with `tracker link`.
8. Export in a format the UI can ingest, with `tracker export`.
