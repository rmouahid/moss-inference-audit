# Portal: notes

The portal part is hands-on use, not a benchmark: signup with GitHub on 2026-11-25, then the task
page of TSK-01 checked on an iPhone at 17:59Z. The screenshots are kept private, because they show
account details.

## What worked

- **Signup.** Signing up with GitHub through the invitation link took a few seconds and needed no
  extra step worth changing.
- **Task page.** One screen shows:
  - the status;
  - time and spend against their caps ("15s of 2 min", "$0.005 of $0.05");
  - steps, tokens and the model;
  - a recording with captions and timestamped screenshots;
  - the JSON result with "Matches the schema";
  - the date the files expire.
- **Agreement with the API.** The page matched the API at the precision it displays: 12,511
  tokens, 2 steps, 15 s, and $0.005 against $0.0053 reported by the API (see TSK-A4 in
  [tasks.md](tasks.md)).

## What could be better

| Observation | Suggestion |
|---|---|
| On a phone the interface feels heavier than needed, and it is hard to see at a glance where things stand. | A simpler mobile layout that puts the current state first. |
| Spend shows 3 decimals: a $0.0053 task reads "$0.005". | Show the total with 4 decimals. |
| Network traffic is billed hours after a task and is not shown on the page, so the spend shown is not the final cost. | A "traffic pending" label until it is billed. |
| The paused recording at 0:09 showed an all-white frame. | Unconfirmed: it may be a real loading moment. Worth a second look. |
| The portal shows "Moss Fast"; the API expects `moss-fast`. | Show the API name next to the display name. |

## Limitations

- One account and one task page were checked.
- The Usage and Billing pages, "Share" and "Run again" were not exercised for this report.
