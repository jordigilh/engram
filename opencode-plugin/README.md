# Engram OpenCode plugin

This package is the versioned production OpenCode/OpenChamber plugin for
[Engram](https://github.com/jordigilh/engram). Production configurations should
pin an exact package version, for example:

```json
{
  "plugins": [
    {
      "package": "engram-opencode-plugin@0.1.0",
      "options": {
        "pluginArtifact": {
          "package": "engram-opencode-plugin",
          "version": "0.1.0",
          "source": "npm"
        }
      }
    }
  ]
}
```

The package embeds the source revision used to build it and reports that
revision at OpenCode startup. See the repository's
[`docs/OPENCODE.md`](../docs/OPENCODE.md) for release, rollback, and explicit
local-development instructions.
