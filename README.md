# Metro Train Updates

Simple app that sends an email update if your configured line is down.

## Quick Start

### Email set up
Use a dedicated Gmail account for sending updates to your emails. Enable 2-Step verification and create a dedicated app password, which will be 16 random characters. You will need to create a .env (or rename the existing file to .env) and include it in the same folder as the python script.

```
METRO_GMAIL_SENDER=<your_configured_email>@gmail.com
METRO_GMAIL_PASSWORD=<16_char_password>
```

### Config File
```
{
  "minutes": 120,
  "include_planned_works": true,
  "recipients": {
    "bob@snailmail.com": [
      {
        "time": "09:00",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "lines": ["Sunbury", "Pakenham"]
      },
      {
        "time": "16:30",
        "days": ["mon", "tue", "wed", "thu", "fri"],
        "lines": ["Glen Waverly"]
      }
    ],
    "alice@snailmail.com": [
      {
        "time": "18:00",
        "days": ["sat","sun"],
        "lines": ["Lilydale"]
      }
    ]
  }
}
```
The above config file is included to show you how to configure time, line, and emails. 

- `recepients` is expandable with additional email addresses, every email address added will receive real email updates from the configured sender account.
- Each recepient list item configures an alert for a set day and time against a line. 
  - `time`, when the alert should be sent.
  - `days`, days of the week the alert should be sent.
  - `lines`, the train lines the alert should concern. Including multiple lines in the list includes all notifications within the same email.
- The `minutes` field can be set for how far ahead the script should consider line impacts. In the example script, Alice will receive emails for impacts on the Lilydale line predicted to affect her journey from 18:00-20:00.

### Cron Job

Set a call up on a cron job in set intervals as below:

```
*/30 * * * * /usr/bin/python3 {$PATH$}/metro_alert.py --config {$PATH$}/config.json >> {$PATH$}/metro.log 2>&1
```

The cron job must occur on the same intervals included in the config file or it will not occur at the right times. i.e. If you have a `time` set for 7:15 and 14:30, your cronjob may need to fire every 15 minutes instead.