# Upstream Coverage

- Source: `https://github.com/twilio/ai`
- Ref: `main` at `aa67a6d476107d6742f31a53d68b10749552930f`
- License classification: `MIT`
- Inventoried `SKILL.md` files: `56`
- Inventory hash: `1bdb513e484c15fafbc8f9d3187d5c239a4c7d2afe4faf72c7fcb45bbcdffb07`

This wrapper intentionally does not expose every upstream subskill as a LocalSetup skill.
Select one upstream path at a time for future import, then run importer, vetter, normalizer, and sandbox validation.

## Inventory Manifest And Hash Recipe

The enumeration root is `.` in the pinned upstream repository. The manifest below
lists every regular `SKILL.md` file recursively beneath that root, using
repository-relative paths and SHA-256 hashes of the raw file bytes. It inventories
skill paths; it does not inventory MCP servers, tools, resources, prompts, or
configuration.

To reproduce the inventory hash, preserve the manifest row order below. For each
row, concatenate its path, one ASCII space, and its lowercase hexadecimal hash.
Join those records with one LF (`\n`), without a trailing newline, encode as UTF-8,
and compute SHA-256. `source_sha256` identifies this aggregate, not an individual
file.


- `skills/sendgrid/twilio-sendgrid-account-setup/SKILL.md` (`de1599908000a8fb6035e1a973ca39fd48c268a56c76bc45136e6999f7252299`)
- `skills/sendgrid/twilio-sendgrid-deliverability-advisor/SKILL.md` (`f008f75a4705071c9d5d0c68d3cdc606a8f748118ffe707192febc22f68faf26`)
- `skills/sendgrid/twilio-sendgrid-email-send/SKILL.md` (`009f20484c8ea8ac7d24c9c99db32dd1cedea9265941f2c48ad63d08f2e96ebc`)
- `skills/sendgrid/twilio-sendgrid-email-settings/SKILL.md` (`30d6c9e98bc3959d31adefc54043fc6f84da7983fd18904da0f39f83e08ca573`)
- `skills/sendgrid/twilio-sendgrid-engagement-quality/SKILL.md` (`4da10ac83d5ef41fba85404f1fcdef4ea5163e3ab18594a3db9a6c1d708b8d1c`)
- `skills/sendgrid/twilio-sendgrid-inbound-parse/SKILL.md` (`754b2e3e0997595e8bde803cd6785c8099fa99976c26fbd6b6b25306467446a3`)
- `skills/sendgrid/twilio-sendgrid-suppressions/SKILL.md` (`2d28ac5258311b07d149b050d16e42e7ca192826d3015219e73ac9595c80a9b5`)
- `skills/sendgrid/twilio-sendgrid-webhooks/SKILL.md` (`24f3cad21170581d228f7dd800e7cc98bd950e02429d53bf6562efc984e084a0`)
- `skills/twilio/twilio-account-setup/SKILL.md` (`e52a985e3d052e7d16c28e1ce56e68041fbe46e161a9298e5ff5ce6fa88eb1b8`)
- `skills/twilio/twilio-agent-augmentation-architect/SKILL.md` (`aed5b8668af93b3e4a68a87fd6cc5f3c7212f9bec66e68599eb53a921ac26d56`)
- `skills/twilio/twilio-agent-connect/SKILL.md` (`3c056f9ec7103958517ba5c59c14d3caa3c336e0dd4c0c4ac755eeb200b86b85`)
- `skills/twilio/twilio-ai-agent-architect/SKILL.md` (`f546f819ac2ead063ce05248b2c03421fffa61879ea6feecfebb4fbb263c3722`)
- `skills/twilio/twilio-call-recordings/SKILL.md` (`b28ac7f3e4fb01af892c5d645ea0ba688933ced02fb59c49cc94c79d11e76a8a`)
- `skills/twilio/twilio-cli-reference/SKILL.md` (`c16543910ee3893bcd53ed3d6b3888bb9232b2e1ef6434a50b342353be2142dc`)
- `skills/twilio/twilio-compliance-onboarding/SKILL.md` (`e7c001794fc6de67f33c7a2be555473ad4027e206841419bf69a93f20fb26762`)
- `skills/twilio/twilio-compliance-traffic/SKILL.md` (`9b6b5789abbdcd3b1f9f46bd2fe103138ddb4cf8452fa99bc0440c606c34bfa8`)
- `skills/twilio/twilio-conference-calls/SKILL.md` (`e1ca152391ea1eaa1a883be185c89376d275d2340579344d3f5419f76fe58f3e`)
- `skills/twilio/twilio-content-template-builder/SKILL.md` (`6fdefa277e821f0fc2abf2e29ad1e2e45ce504d606196e47ccccd83fb27e8c29`)
- `skills/twilio/twilio-conversation-intelligence/SKILL.md` (`fe1a0c38d64fbd6dbbd679fbe3cace05858602175d45f7732a1caff449fd554a`)
- `skills/twilio/twilio-conversation-memory/SKILL.md` (`1b35849b61ca3924a09e51fa237db91fc2c400540c38bf0ade825f9e8dadbf8f`)
- `skills/twilio/twilio-conversation-orchestrator/SKILL.md` (`30885b496888cdd1a3e894c89821328cf1c3d1688746675ed1d3a45a7a90660e`)
- `skills/twilio/twilio-conversations-classic-api/SKILL.md` (`8f793162402dd6a26cfa597e77b83cf021aff22d8aaeadcd5360acd90960d4d4`)
- `skills/twilio/twilio-customer-support-architect/SKILL.md` (`fec13499fbca072f3416078e767d5bafbffad3f32e3a183083e6d2ad8b85e0bb`)
- `skills/twilio/twilio-debugging-observability/SKILL.md` (`d952cadae46dd7e95cec24347a2db04607113aeb28055c88ada2b8505d2e4fdc`)
- `skills/twilio/twilio-email-deliverability-advisor/SKILL.md` (`c8b60959bc923071f5bb321fe53294ce2e49de0401cd52003ffb067914b15ec9`)
- `skills/twilio/twilio-email-send/SKILL.md` (`8aa92d66301d73d9ec9a12396f9f4f0b6d626826ca8e639f7253022d93b61468`)
- `skills/twilio/twilio-enterprise-knowledge/SKILL.md` (`b24af6ac3feb453db1955732c0914fdc6d4bc778292e58cc43ecf2530ecbb81e`)
- `skills/twilio/twilio-iam-auth-setup/SKILL.md` (`76639649f7563496b0b102f9f21fd000b47ec061eb9b0d64704532fc2f0a804a`)
- `skills/twilio/twilio-identity-verification-advisor/SKILL.md` (`95f965612d00c3fee192a117c6cb42b221ed4cdb2e3f2e1c269ce7c8812435ba`)
- `skills/twilio/twilio-lookup-phone-intelligence/SKILL.md` (`08f7feb10ae096b497d1d282a5b247fd3a1d6613ef7f97ad42cc48c85c7ed7bd`)
- `skills/twilio/twilio-marketing-promotions-advisor/SKILL.md` (`11e04c0838ba1772d5bfe16b21d242032220395a277874826ae92365a0634211`)
- `skills/twilio/twilio-messaging-channel-advisor/SKILL.md` (`c7854ff6b7441ca562d91092e1da49c9c944050d6f6095d53c3ada1c2712892b`)
- `skills/twilio/twilio-messaging-overview/SKILL.md` (`369760f189f938e33142c1ca11b3cef352b3933eee7213ba1d155c8f2027d3cf`)
- `skills/twilio/twilio-messaging-services/SKILL.md` (`a97ec167553e1d1832e8a8a13d29ad40fcbcab65ef56f873d1f659937dda67a0`)
- `skills/twilio/twilio-messaging-webhooks/SKILL.md` (`2d7abf91c32ef205f9653f1fe859e15cd4cc355b204e271e27f7e4d1dcddff2a`)
- `skills/twilio/twilio-notifications-alerts-advisor/SKILL.md` (`8018f66e1123aecdcf2d9b8d627438f1c8b597303f5802c73f7f658fb840310b`)
- `skills/twilio/twilio-numbers-senders/SKILL.md` (`de7b4c12d3e686509a0e21d3567dd5afe7d72e5ee6803b96632407859ad7b1de`)
- `skills/twilio/twilio-organizations-setup/SKILL.md` (`6358ae1d05ccebb8cf48c736e0e68133602372a7aa1e5a1527a757912faff5b3`)
- `skills/twilio/twilio-rcs-messaging/SKILL.md` (`9754a2b3c332e9f852793270e9c22e5ff3321bed828ff616d23f02889fcdb76a`)
- `skills/twilio/twilio-regulatory-compliance-bundles/SKILL.md` (`c5877b728fb7353611ad7b78d33211ee7084a178a5ac58668f952e6e70f19251`)
- `skills/twilio/twilio-reliability-patterns/SKILL.md` (`0ca7d88af0b2b04d369c770ec6f39125b488ec0083cad56ed975d7356be635d6`)
- `skills/twilio/twilio-security-api-auth/SKILL.md` (`7d8aa17b82c1a3cd450643170cdbb172524bc1b47242073de3967fb8b34e069d`)
- `skills/twilio/twilio-security-compliance-hipaa/SKILL.md` (`7e5ec753e5e7ce8ff43e069f2514c7136385b88d23aabb05ca86db250187802f`)
- `skills/twilio/twilio-security-hardening/SKILL.md` (`caf51cc0f54a239d8e6dffda1c1ba0c2610132eabe686749fbec009e7b01471b`)
- `skills/twilio/twilio-send-message/SKILL.md` (`cb2605eb20f6c4f37a5ab3d1137d2cf19f4ba21634d24ca4b125dfcfe15add93`)
- `skills/twilio/twilio-sms-isv-setup/SKILL.md` (`053b358c2f28b558534a2671b3bc3f427ccddb452294b64a1281e8da3e1bcf6d`)
- `skills/twilio/twilio-sms-send-message/SKILL.md` (`115fd1a596ac8bc1ab756785a76c752960487f09eab0d3b08556262e3a32d85f`)
- `skills/twilio/twilio-studio-flows/SKILL.md` (`a218662edca51ddf3491a999ea3b740eb9184a829cce96edcc16cfee4f1c80e6`)
- `skills/twilio/twilio-taskrouter-routing/SKILL.md` (`290218792a1ad0f5967650fd9e3f0e11275ab55bfcb2f0757282b887a9962aac`)
- `skills/twilio/twilio-verify-send-otp/SKILL.md` (`bd0606a43e40ef9c9bc4bd7f9f1facb2c6d575bbc14fd8f5192e8419b12b8b74`)
- `skills/twilio/twilio-voice-conversation-relay/SKILL.md` (`f0d6cd430779ff49bc2298a20b9565d60ea5c220d4c90d7618a49bda011caef2`)
- `skills/twilio/twilio-voice-outbound-calls/SKILL.md` (`389ee1901e5d32bbca997d65dd4abb984997fa296af225d863711ed71615dd00`)
- `skills/twilio/twilio-voice-twiml/SKILL.md` (`60d5495a42a9132ac29138362208b2c87027eec79a4d2b4dc521b32ff4dcdec8`)
- `skills/twilio/twilio-webhook-architecture/SKILL.md` (`82147ea632fe53d3fb674d374dd88ed117b0c978db96341bbdba70013d2b4999`)
- `skills/twilio/twilio-whatsapp-manage-senders/SKILL.md` (`0aacd6ecc0d89b1f7287e458b79d5e10208ba025adafca7f1c96f65dcef76cf8`)
- `skills/twilio/twilio-whatsapp-send-message/SKILL.md` (`04ac30cd716b5300fe3ac7adca05aa88a35204175a70825b41d6be404e132a13`)
