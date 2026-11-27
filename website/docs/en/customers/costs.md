# Costs and licences

**Knowledge & admin → Costs & licences** shows what each customer costs you per month, as the basis for your invoice,
and keeps everything with a price or an end date in one place.

## Step 1: fill in the price list

Open the tab **Price list** and enter monthly prices for:

- each vCPU, GB of memory and GB of disk given to the customer's VMs and containers;
- each managed machine (server, workstation, Proxmox VE node);
- each licensed Microsoft 365 user and each Microsoft 365 licence type.

A customer can have its own price for any line, which wins over the default. Add a purchase price to see your margin.
Microsoft 365 licences that a tenant has but that have no price yet are listed, so nothing is forgotten.

## Step 2: add licences, contracts and hardware

On **Licences & contracts** add software licences, contracts (support, internet, hosting, lease), hardware with
warranty, services and one-off charges. For each: vendor, reference, serial number, the machine it belongs to,
quantity, sale and purchase price, and how it is billed (monthly, quarterly, yearly, once, or only tracked).

Give contracts an **end date**, a **notice period** and whether they **renew automatically**. Stackpilot warns before
the end date and before the last day to cancel (60 days ahead by default), and moves the end date on when a contract
renews by itself. **Import hardware** makes an item for every machine with a model or serial number.

## Step 3: the monthly statement

Every hour Stackpilot records what each customer uses. The **Monthly overview** per customer shows the lines of the
month (resources, machines, Microsoft 365, then items charged that month), discount, VAT, totals and margin.

1. Check the month. It is calculated live.
2. Click **Make final**: the lines are frozen, so the invoice does not change when prices change later. You can reopen
   it.
3. Export per customer as **PDF** or **CSV**, or all customers of a month in one CSV for your bookkeeping.

Per customer you set whether it is billable at all (your own environment is not), the debtor number, a discount,
which parts are billed and a note on the statement.

## Warnings

Contracts, licences and warranties that run out raise an alert and appear in the morning summary, together with
statement lines without a price. The monthly customer report can include the statement (off by default).
