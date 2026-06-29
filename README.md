# Hertz AAA Rental Monitor

This repo runs a daily GitHub Action that checks Hertz rental pricing near Fairfax, Virginia, prioritizes AAA-related rate context, and emails the current best result.

## What it does

- Runs on GitHub Actions so your computer does not need to stay on
- Searches Hertz booking flow with Playwright
- Emails a daily summary to your chosen recipient
- Uploads HTML and screenshot artifacts when Hertz changes or quote extraction is partial

## Required GitHub Secrets

Add these in:
`Settings -> Secrets and variables -> Actions`

### Required

- `GMAIL_ADDRESS`
- `GMAIL_APP_PASSWORD`
- `ALERT_RECIPIENT`

### Recommended

- `AAA_ZIP_CODE`
- `TARGET_TOTAL_USD`

### Optional

- `HERTZ_LOCATION_CANDIDATES`
  - JSON array, for example:
  - `["Fairfax, VA","Dulles - Dulles International Airport (IAD)","Washington, DC - Ronald Reagan Washington National Airport (DCA)"]`
- `HERTZ_DATE_OFFSETS`
  - JSON array, for example:
  - `[0,1,2,3,7,14]`
- `HERTZ_RENTAL_LENGTHS`
  - JSON array, for example:
  - `[28,30,31,35]`

## Schedule

The workflow currently runs at:

- `13:00 UTC`

That equals:

- `9:00 AM EDT`
- `8:00 AM EST`

If you want exact `9:00 AM` Eastern all year, the workflow can be adjusted later.

## First-time setup

1. Push this repo to GitHub.
2. Add the secrets.
3. Go to the `Actions` tab.
4. Run `Hertz AAA Rental Monitor` manually once.
5. Confirm the email arrives.

