## Why

The Veckobrev sensor reads the learning log (`/learnlog/learnlog/appData`). For a Förskoleklass
child at a Stockholm school that section is not permitted, so the sensor stays Unknown. The
weekly letters exist in the Information (news) feed as items titled "Veckobrev v 43",
"Veckobrev v 42" and so on (seen live: 15 news items, most of them weekly letters).

## What changes

- When the learning log is not permitted, the weekly-letter sensor falls back to the newest news
  item whose title starts with "Veckobrev": title, date, text.
- A child that has neither gets no weekly-letter sensor.
- Review what else a Förskoleklass child can see (today only the name and "Frivillig läxa") and
  decide which sensors to hide or add.

## Open

- One shared sensor on the account device or one per child.
- Whether the title match should be configurable for schools that name it differently.
