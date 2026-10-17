# Engram OpenCode plugin

This is the source and build definition for the versioned production
OpenCode/OpenChamber plugin for [Engram](https://github.com/jordigilh/engram).
Production uses a checksummed GitHub Release artifact installed under
`~/.engram/opencode-plugins/`, not npm or a mutable checkout. The release
artifact contains a self-contained bundle, so OpenCode does not need to install
plugin dependencies.

After installing a release artifact, production configuration should point at
its exact directory:

```json
{
  "plugins": [
    {
      "package": "/Users/<user>/.engram/opencode-plugins/engram-opencode-plugin-0.1.0",
      "options": {
        "pluginArtifact": {
          "package": "engram-opencode-plugin",
          "version": "0.1.0",
          "source": "github-release"
        }
      }
    }
  ]
}
```

The artifact embeds the source revision used to build it and reports that
revision at OpenCode startup. See the repository's
[`docs/OPENCODE.md`](../docs/OPENCODE.md) for release, rollback, and explicit
local-development instructions.
