# calsync privacy policy

*calsync is open-source software that runs on your own computer or your own server. The people who wrote it do not run any calsync service and receive no data from it.*

## What calsync does with your data
- **Stored on your device only.** Your settings, keys and sign-in tokens are saved in a file called `config/calsync.env` on the computer or server where you run calsync. Nothing is uploaded to the calsync authors.
- **Google Calendar.** If you connect your Google account, calsync asks Google for permission to read and edit your calendar events. It stores one refresh token on your device and uses it to talk directly to Google's API from your device. You can revoke access at any time at <https://myaccount.google.com/permissions>.
- **Other services you choose.** Photos, documents and text you submit are sent to the AI service you select (Gemini, Claude, or a model on your own machine). Events you add are sent to your Notion workspace. If you set them up, calsync also talks to your Immich server and your email server. Each of those services has its own privacy policy.
- **Update checks.** calsync asks GitHub whether a newer release exists. This request does not include your data.
- **No analytics, no ads, no tracking, no selling or sharing of data.**

## Google API Services User Data Policy
calsync's use and transfer of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including its Limited Use requirements. Calendar data is used only to show and edit your own events inside calsync on your own device.

## Contact
Open an issue on this repository, or use the private reporting described in [SECURITY.md](SECURITY.md).
