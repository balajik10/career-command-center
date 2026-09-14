# Native job alerts and local imports

Restricted platforms are covered through alerts you create yourself, exports you initiate, and manual imports. The application never logs in to LinkedIn, Naukri, Indeed, Glassdoor or Wellfound, never automates their searches/messages, and never fetches their pages from imported alert links.

Use broad queries before using scarce company-specific alerts. Provider alert limits vary and were not all verified during this build; follow each platform's current UI limit instead of assuming unlimited alerts. Suggested query groups:

| Role group | Terms to enter/adapt | Location filter |
|---|---|---|
| Early-career engineering | Software Engineer I, SDE I, Associate Software Engineer, Junior Software Engineer | Bengaluru / India |
| Backend Java | Backend Engineer, Java Developer, Spring Boot Engineer | Bengaluru / India remote |
| Development-heavy infrastructure | Platform Engineer, Infrastructure Software Engineer, Production Engineer | India / remote within India |
| Search/data systems | Search Engineer, Ranking, Distributed Systems, API Platform | Bengaluru / India |
| Broader experience language | 0–2 years, 1+ years, 2+ years | India; inspect actual requirements |

Near-match experience remains discoverable with an explicit gap. Avoid senior/lead/staff scope and roles dominated by frontend, support, manual QA or non-technical operations. A title numeral alone is not reliable experience evidence. Use employer watches for the companies you personally prioritize; keep that private watchlist in the Sheet.

For Google Alerts/manual discovery, combine a verified official company domain with role and location, for example `"Backend Engineer" "Bengaluru" site:careers.example.com` or `"Spring Boot" "0-2 years" "India"`. Substitute an observed official domain. New companies stay pending until individually reviewed.

Route owned alert emails into a dedicated folder/label such as `JobRadar/Incoming`. A dedicated mailbox reduces accidental access to unrelated mail. Gmail's readonly OAuth permission is mailbox-wide; a label is an application filter rather than an authorization boundary. Live Gmail/IMAP message collection is disabled in this release.

Export owned emails as `.eml` files into a private local directory. Configure `GMAIL_SENDER_ALLOWLIST` with the exact observed sender addresses you trust, and configure the private HMAC key before importing:

```bash
uv run career-radar import eml .private/alerts
```

The local parser reads allowlisted HTML links, stores minimal metadata and a private HMAC message identifier, ignores remote images, and discards token-bearing redirect URLs. It does not visit any link, mark messages read, move mail, delete mail, or retain full email content in the Sheet. Plain-text-only or changed templates can require a manual CSV/JSON import. A generic extracted link remains a lead until its role/company details and official application destination are confirmed.

For manual job imports, use columns `id`, `company`, `title`, `url`, with optional `apply_url`, `location`, `description` and `posted_at`, then run `career-radar import jobs PATH`. Do not include guessed salaries, people, referral willingness or posting times. Imported timestamps and source URLs remain evidence, not instructions to execute.
