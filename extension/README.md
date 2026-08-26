# Job Pipeline Autofill — Chrome extension

Fills job applications from the local pipeline server. **It never submits.**

## Why this exists

The pipeline UI is served from `127.0.0.1:8765`. An application form lives on
`greenhouse.io`. Same-origin policy means a page on one cannot read or write a
single field on the other — that is a browser security rule, not a gap in the
code. So the click has to be handed to something with browser-level privileges.
This extension is that something.

## Install (one time, about a minute)

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked**
4. Pick this folder: `C:\Users\adoxe\Desktop\Claude\job-pipeline\extension`

No store listing, no review, nothing leaves the machine.

## How it works

1. You click **Auto Apply** in the pipeline UI.
2. The UI parks that job on the server and opens the application in a new tab.
3. The extension's content script wakes on the ATS page, asks the server what
   is pending, checks the URL matches, and fills the fields.
4. A banner reports what it filled, what is still blank, and what it skipped.

## What it will not do

- **Submit.** No code path clicks a submit control. There will not be one.
- **Accept terms, tick consent boxes, or create accounts.**
- **Attach your resume.** Browsers forbid a script from setting a file input, so
  that a page cannot read your disk. The banner shows the resume path; the
  attach is one manual click.
- **Fill salary, SSN, date of birth, licence, passport, bank details, passwords,
  or signature fields.** Those are refused in the packet itself, and the reason
  travels with them.

## Scope

Runs only on `greenhouse.io`, `lever.co` and `ashbyhq.com` job pages, and talks
only to `127.0.0.1:8765`.

The fetch to the local server happens in the background service worker rather
than the content script. A content script's fetch obeys the page's CORS rules;
a service worker with host permissions does not. That means the local server
never has to send permissive CORS headers, so a random website cannot ask it
for the packet — which matters, because the packet has your address and phone
number in it.

## If nothing happens

- Is `python jobs.py serve` running?
- Did you click Auto Apply within the last 10 minutes? The pending slot expires,
  so a form opened hours later is not filled behind your back.
- Is the tab actually on the application page the button opened?
- `chrome://extensions` → this extension → **service worker** → Console, for errors.

The pipeline UI also keeps a manual fallback: expand "Manual fallback" under the
fill packet for a prompt you can hand to Claude in Chrome instead.
