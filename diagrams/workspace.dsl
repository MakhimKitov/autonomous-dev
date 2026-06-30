workspace "Autonomous Maintenance Platform" "Autonomous AI developer platform that maintains a product (Discord bot) through GitHub." {

    model {
        !impliedRelationships true

        // People
        humanOwner = person "Human Owner" "Triages issues (ready label), reviews & merges PRs, deploys prod."
        discordUsers = person "Discord Users" "Real users of the product; their messages are untrusted input."

        // System in focus
        platform = softwareSystem "Autonomous Maintenance Platform" "Runs autonomous AI developers that maintain the product through GitHub." {
            controlPlane = container "Control Plane / Scheduler" "Triages and assigns ready issues, spawns burst containers, enforces budgets & kill-switch, owns sessions." "Python service"
            devAgent = container "Dev Agent (burst worker)" "Per-burst: clone, branch, code, test, self-review, open PR; resumes its session." "Docker + claude-agent-sdk"
            monitor = container "Monitor" "Watches telemetry and files structured issues (auto provenance)." "Python service / separate repo"
            store = container "Session & Run Store" "Resumable Claude sessions + run telemetry / violations ledger, keyed by issue." "S3 / Postgres" {
                tags "Database"
            }
        }

        // External systems
        bot = softwareSystem "Discord Bot — Product" "The maintained product; live in prod with real traffic." {
            tags "External"
        }
        github = softwareSystem "GitHub" "Issues, PRs, branch protection, CI/CD — the integration substrate / source of truth." {
            tags "External"
        }
        claude = softwareSystem "Claude (Anthropic API)" "LLM engine behind the dev agent & monitor." {
            tags "External"
        }
        codex = softwareSystem "Codex" "Independent pre-PR code reviewer (runs in-container)." {
            tags "External"
        }
        copilot = softwareSystem "GitHub Copilot" "Independent post-PR code reviewer (on the PR)." {
            tags "External"
        }
        telemetry = softwareSystem "Product Telemetry" "Logs, metrics & Alertmanager signals from the live bot." {
            tags "External"
        }

        // People relationships
        humanOwner -> github "Triages (ready label), reviews & merges PRs"
        discordUsers -> bot "Send messages" "Discord"

        // Platform internals
        controlPlane -> github "Polls ready issues, assigns, tracks PRs" "API"
        controlPlane -> devAgent "Spawns burst, injects task + session" "Docker"
        controlPlane -> store "Loads/saves sessions & run telemetry"
        devAgent -> github "Clone, push branch, open PR" "git / API"
        devAgent -> claude "Drives the agent loop" "API"
        devAgent -> codex "Pre-PR self-review"
        monitor -> telemetry "Watches" "read-only"
        monitor -> github "Files issues (auto provenance)"

        // External ecosystem
        copilot -> github "Reviews PRs"
        github -> bot "CI tests & deploys on merge"
        bot -> telemetry "Emits logs / metrics"
    }

    views {
        systemLandscape "Landscape" "C0 (ecosystem) — the whole experiment" {
            include *
            autoLayout lr
        }

        systemContext platform "Context" "C0 — Autonomous Maintenance Platform in its environment" {
            include *
            autoLayout lr
        }

        container platform "Containers" "C1 — inside the Autonomous Maintenance Platform" {
            include *
            autoLayout lr
        }

        styles {
            element "Person" {
                shape Person
                background #08427B
                color #ffffff
            }
            element "Software System" {
                background #1168BD
                color #ffffff
            }
            element "Container" {
                background #438DD5
                color #ffffff
            }
            element "Database" {
                shape Cylinder
            }
            element "External" {
                background #999999
                color #ffffff
            }
        }
    }
}
