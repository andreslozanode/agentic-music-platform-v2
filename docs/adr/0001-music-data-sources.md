# ADR 0001 - Music data sources after the 2026 Spotify and Reddit restrictions

- Status: accepted
- Date: 2026-09
- Deciders: platform team, AI governance

## Context

The requirement was "latest tracks, new artists, playlists and the global Top 50 of the last
month", originally assuming Spotify. Verified as of September 2026:

- Spotify's February/March 2026 changes removed, for apps in Development Mode, the browse and
  new-releases endpoints, artist top tracks, other users' playlists and profiles, and batch
  endpoints; `popularity` and follower fields are gone and search is capped at 10 results.
  Extended access requires a business review, and even then the editorial "Top 50 Global" playlist
  is not retrievable through the API.
- Reddit no longer offers self-service OAuth (Responsible Builder Policy, November 2025) and
  unauthenticated `.json` endpoints return `403` since 30 May 2026.

## Decision

1. Use **ListenBrainz** as the primary source: fresh releases and *sitewide* charts with
   `range=month` give a real monthly global ranking from open data.
2. Use the **Deezer public API** for charts, editorial releases and playlists, and as a second
   opinion: the Jaccard overlap between the two charts is published as a data-quality and
   responsible-AI signal.
3. Use **Spotify only for search**, optionally, to add links when credentials exist.
4. Keep **Reddit** for level 1 with approved read-only OAuth credentials; never scrape, never use
   the unauthenticated JSON endpoints.
5. Ship a **fixtures mode** with synthetic data for tests, demos and CI, rejected in production by
   configuration validation.

## Consequences

- The product can never claim to publish "the Spotify Top 50". Every answer states the source, the
  period and the population bias; the system card repeats it.
- Chart numbers reflect ListenBrainz listeners, which skew towards self-hosted music trackers.
  Cross-source agreement with Deezer is monitored and flagged when it drops.
- If Spotify grants extended access later, the source layer already has a Spotify client and the
  aggregation service can promote it without touching the agent or the pipeline.
