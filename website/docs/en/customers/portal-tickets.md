# Customer portal and tickets

Customers can report problems and questions in their own portal. Stackpilot links each ticket to the right
machines and immediately runs a first diagnosis, so you know where to look before you open the ticket.

## Switch the portal on

1. Open **Settings → Customer portal**.
2. Switch on **Customer portal on**, choose who gets a mail about new tickets (the assignee, a fixed address or every
   admin) and write a welcome text.
3. The portal is at `https://<your address>/portal`.

## Give customers an account

1. Open **Tickets → Portal accounts** (or the customer's page).
2. Click **Add account**, pick a contact person and choose the role:
    - **Member**: sees the tickets they reported.
    - **Manager**: sees every ticket of the organisation, and can get a mail about each one.
3. The contact gets an invitation mail to choose a password (valid 7 days). You can resend it, send a password link,
   switch an account off or delete it.

Portal accounts are separate from staff accounts: they can only reach the portal, and only ever their own
organisation.

!!! tip "See what the customer sees"
    **View as customer** in the top bar opens the portal of the picked customer as its managers see it, read-only.

## What the customer does in the portal

- Report a problem, question or request with a priority, and pick the computers or servers it is about.
- Follow the status, reply, and close a ticket.
- See planned and recent maintenance under **Maintenance**, and subscribe to it as a calendar.

The portal is in English and Dutch, light and dark, and works on a phone.

## Handling tickets

Open **Today → Tickets**. The menu badge shows how many tickets wait for a reply.

1. Open a ticket. The **first diagnosis** is already there: the linked machines were checked (problems found, failed
   services, recent errors, busiest processes), with open alerts and uptime checks that are down, and suggested next
   steps. Only staff see it. **Run again** repeats it.
2. Set the status (new, open, waiting on customer, on hold, resolved, closed), priority, type and assignee.
3. **Reply** to the customer, or add an **internal note**. A reply can change the status in one go ("ask and wait",
   "resolve").
4. A customer called? Click **New ticket** and open one for them.

Mails go through your own mail server, in the customer's language. Resolved tickets close by themselves after a
number of days without a reply.

## With the AI

The AI can list and read tickets, run the diagnosis, and (scope `tickets`) reply, add notes, change and open tickets.
For example: *"Look at the new tickets of Acme and tell me which ones you can solve."*
