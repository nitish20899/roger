# Research: how a bot can hear / speak / post chat in Teams and Google Meet (Sept 2026)

Legend: **[V]** verified in an official doc/vendor page · **[U]** uncertain / inferred.

## 1. Google Meet

### Meet Media API (native, WebRTC)
- Still Developer Preview. Cloud project, OAuth principal, **and every participant** must be enrolled in the Workspace Developer Preview Program. [V] https://developers.google.com/workspace/meet/media-api/guides/overview
- **Receive-only**: consume audio/video streams and participant metadata. No send path, no chat. [V] Recall.ai's wrapper confirms "no sending messages or Output Media". https://docs.recall.ai/docs/meeting-direct-connect-for-google-meet-media-api
- Per-participant audio capped at 3 virtual streams (SFU forwards 3 most relevant speakers; CSRC identifies speaker). [V] https://developers.google.com/workspace/meet/media-api/guides/virtual-streams
- Admin must enable under Meet safety settings; consent by host; participants notified. [V] https://knowledge.workspace.google.com/admin/meet/control-media-api-access-in-google-meet
- Verdict: hard blocker for a speaking bot.

### Meet REST API / Add-ons / chat
- REST API (GA) = conference records, participants, recordings, transcripts. **No endpoint to post an in-meeting chat message.** [V] https://developers.google.com/workspace/meet/api/guides/overview
- Add-ons SDK = side-panel/main-stage iframes; no chat-posting API. [V]
- Only way to post to Meet chat is a browser-participant bot that types into the chat UI.

### Headless-Chromium guest bot
- **Blocker since Mar/Apr 2026: "Safeguarded guest admit flow."** Risky-looking connections (data-center bots) land in a "potential threats" queue whose default action is deny. No admin override. [V] https://workspaceupdates.googleblog.com/2026/02/safeguarded-guest-admit-flow-in-google-meet.html
- Mitigation used by vendors: **signed-in bots** with dedicated Workspace accounts (MeetingBaaS: paid Workspace + SAML SSO, one user per concurrent bot). [V] https://www.meetingbaas.com/en/blog/authenticated-bots-google-meet
- Headless fingerprinting and pre-join mic/camera prompts trap naive bots. [V] https://www.recall.ai/blog/how-i-built-an-in-house-google-meet-bot

## 2. Microsoft Teams

### Graph Cloud Communications + application-hosted media (only native real-time audio path)
- Permissions `Calls.JoinGroupCall.All` + `Calls.AccessMedia.All` (admin consent), Azure Bot registration, manifest `supportsCalling`, public webhook; join via `createCall` with meeting `joinUrl`. [V] https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/calls-meetings-bots-overview
- Hard constraints: **C#/.NET only**; media NuGet must be ≤3 months old; **Windows Server in Azure** (VM/VMSS/AKS Windows nodes), instance-level public IP + cert; media pinned to the accepting VM; platform still "developer preview". [V] https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/requirements-considerations-application-hosted-media-bots
- Microsoft (Mar 2026): no Python/REST/WebSocket path exists or is planned. [V] https://learn.microsoft.com/en-au/answers/questions/5807336/
- Receive: 20 ms PCM frames; unmixed per-participant via `ReceiveUnmixedMeetingAudio` (max 4 dominant speakers). Send: PCM + H.264 back (full duplex). [V]

### Posting to the Teams meeting chat
- Graph `POST /chats/{id}/messages` app-only permission is migration-only; delegated needs a user token. [V] https://learn.microsoft.com/en-us/graph/api/chat-post-messages
- Working app-only path = **Bot Framework** app installed in the meeting; `sendActivity` into the meeting chat; RSC permissions in manifest. [V] https://learn.microsoft.com/en-us/microsoftteams/platform/apps-in-teams-meetings/meeting-apps-apis
- Tenant blocker: default `ExternalBotAccessMode = RequireApprovalWhenDetected` forces external bots into the lobby; organizer must admit. Anonymous join disabled → guest bots cannot join. [V] https://learn.microsoft.com/en-us/microsoftteams/manage-external-bots · https://docs.recall.ai/docs/signed-in-teams-bots-overview

### Azure Communication Services
- Call Automation bidirectional streaming is good (PCM 16/24 kHz, unmixed up to 4 speakers) **but cannot join existing Teams meetings from the outside**. [V] https://learn.microsoft.com/en-us/azure/communication-services/concepts/call-automation/call-automation-teams-interop

## 3. Bot-as-a-service vendors

| Vendor | Platforms | Real-time audio in | Per-participant | Bot speaks | Chat send | Video tile | Price |
|---|---|---|---|---|---|---|---|
| **Recall.ai** [V] | Zoom, Meet, Teams, Webex, GoTo | WS, 16-bit PCM 16 kHz | Yes (up to 16 speakers; needs 4-core bot) | **Output Media**: bot renders your webpage as camera/screenshare and streams its audio; `Output Audio` for mp3 clips | Yes (Meet 500 chars; Teams 4096 chars, needs chat-enabled tenant or signed-in bot) | Yes (webpage = camera) | $0.50/hr base; 4-core $0.60/hr; +$0.15/hr their transcription; 5 free hrs |
| **Attendee** (OSS, hosted option) [V] | Zoom, Meet, Teams | WS, base64 PCM 8/16/24 kHz | Yes (since Jan 2026) | Yes: WS `realtime_audio.bot_output` PCM; `/speech`; `/output_audio` | Yes | Yes (image; voice-agent via webcam) | Hosted $0.50/hr → $0.35/hr; self-host free |
| **MeetingBaaS** [V] | Zoom, Meet, Teams | WS bidirectional (Speaking Bots, Pipecat-based) | [U] | Yes | join message only; mid-meeting send [U] | image | ~$0.35–0.50/hr + $0.10/hr per stream direction |
| **Vexa** (OSS) [V] | Meet, Teams, Zoom | transcripts only | n/a | No | No | No | $0.30/hr |
| **Skribby** [V/U] | Zoom, Teams, Meet | live events/transcripts | [U] | [U] | planned | ? | $0.35/hr |
| **Nylas Notetaker** [V] | Meet, Teams, Zoom | post-call only | No | No | No | No | n/a |

Recall docs: https://docs.recall.ai/docs/stream-media · https://docs.recall.ai/docs/sending-chat-messages · https://docs.recall.ai/docs/how-to-get-separate-audio-per-participant-realtime · https://www.recall.ai/pricing
Attendee: https://attendee.dev/changelog · https://docs.attendee.dev/guides/realtimeaudio · https://attendee.dev/pricing
MeetingBaaS: https://docs.meetingbaas.com/speaking-bots · https://github.com/Meeting-Baas/speaking-meeting-bot

## 4. Frameworks
- Pipecat: no official Recall/Meet/Teams transport; community pattern = `FastAPIWebsocketTransport` + custom serializer over Recall's `audio_separate_raw.data`. https://github.com/pipecat-ai/pipecat/issues/3272
- LiveKit Agents: no meeting-platform transport; SIP only. https://community.livekit.io/t/integrating-livekit-agents-with-microsoft-teams/419

## Recommendation
- **Primary: Recall.ai** — the only vendor with verified docs for all four capabilities (per-participant real-time PCM, speaking via Output Media, chat send, video tile) on both Teams and Meet. Plan for signed-in bot identities (Meet threat queue, Teams external-bot policy). Measure Output Media latency in a POC; no ms SLA is published.
- **Alternative: Attendee** (open source, self-hostable) — near parity, cheaper, thinner Teams docs for the realtime-output path.
- **Do not build native for the MVP.** Meet has no speaking/chat API at all; Teams native is a C#/Windows-Server/Azure media bot plus a Bot Framework app for chat.

Uncertain: exact Recall Output Media end-to-end latency; Recall's authenticated Meet bot offering; MeetingBaaS mid-meeting chat + per-participant audio; Attendee realtime output on Teams; Skribby speaking.
