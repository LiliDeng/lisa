import * as tl from "azure-pipelines-task-lib/task";
import { HttpClient } from "typed-rest-client/HttpClient";

const marker = "<!-- ai-test-selection -->";

export interface Comment {
    id: number;
    body: string;
    user: { id: number };
}

function positiveInteger(value: string, label: string): number {
    const result = Number(value);
    if (!/^[1-9][0-9]*$/.test(value) || !Number.isSafeInteger(result)) {
        throw new Error(`Invalid ${label}.`);
    }
    return result;
}

function isComment(value: unknown): value is Comment {
    return typeof value === "object" && value !== null &&
        "id" in value && typeof value.id === "number" &&
        Number.isSafeInteger(value.id) && value.id > 0 &&
        "body" in value && typeof value.body === "string" &&
        "user" in value && typeof value.user === "object" && value.user !== null &&
        "id" in value.user && typeof value.user.id === "number" &&
        Number.isSafeInteger(value.user.id) && value.user.id > 0;
}

export function cleanupIds(
    comments: Comment[], authorId: number, buildId: number, attempt: number
): { keep: number; remove: number[] } {
    const reports = comments.flatMap(comment => {
        if (comment.user.id !== authorId ||
            !comment.body.startsWith(`${marker}\n## AI Test Case Selection\n`)) {
            return [];
        }
        const match = /[?&]buildId=([1-9][0-9]*)(?=[)&\s]|$)/.exec(comment.body);
        if (!match) {
            return [];
        }
        return [{ comment, build: positiveInteger(match[1], "report build ID") }];
    });
    const currentMarker = `<!-- ai-test-selection-run:${buildId}:${attempt} -->`;
    if (!reports.some(report =>
        report.build === buildId && report.comment.body.includes(currentMarker))) {
        throw new Error("No report from this job attempt found; old comments were not touched.");
    }
    // A late older build must not replace or delete the report from a newer build.
    reports.sort((a, b) => b.build - a.build || b.comment.id - a.comment.id);
    return {
        keep: reports[0].comment.id,
        remove: reports.slice(1).map(report => report.comment.id)
    };
}

async function getJson(client: HttpClient, path: string, token: string): Promise<unknown> {
    const response = await client.get(`https://api.github.com${path}`, {
        Authorization: `Bearer ${token}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    });
    if (response.message.statusCode !== 200) {
        throw new Error(`GitHub GET ${path} failed: HTTP ${response.message.statusCode}.`);
    }
    return JSON.parse(await response.readBody()) as unknown;
}

export async function run(): Promise<void> {
    const endpoint = tl.getInput("gitHubConnection", true)!;
    const auth = tl.getEndpointAuthorization(endpoint, false);
    if (!auth || auth.scheme !== "OAuth" || !auth.parameters.AccessToken) {
        throw new Error("An existing GitHub OAuth service connection is required.");
    }
    const token = auth.parameters.AccessToken;
    tl.setSecret(token);
    const repository = tl.getInput("repositoryName", true)!;
    if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)) {
        throw new Error("Invalid GitHub repository.");
    }
    const pr = positiveInteger(tl.getInput("id", true)!, "PR number");
    const buildId = positiveInteger(tl.getInput("buildId", true)!, "build ID");
    const attempt = positiveInteger(tl.getInput("jobAttempt", true)!, "job attempt");
    const client = new HttpClient("lisa-pr-comment-cleanup", undefined, {
        proxy: tl.getHttpProxyConfiguration() ?? undefined,
        allowRetries: true,
        maxRetries: 2
    });
    try {
        const user = await getJson(client, "/user", token);
        if (typeof user !== "object" || user === null || !("id" in user) ||
            typeof user.id !== "number" || !Number.isSafeInteger(user.id) || user.id <= 0) {
            throw new Error("GitHub OAuth identity is invalid.");
        }
        const comments: Comment[] = [];
        for (let page = 1; ; page++) {
            const batch = await getJson(
                client, `/repos/${repository}/issues/${pr}/comments?per_page=100&page=${page}`, token
            );
            if (!Array.isArray(batch) || !batch.every(isComment)) {
                throw new Error("GitHub returned an invalid comment page.");
            }
            comments.push(...batch);
            if (batch.length < 100) {
                break;
            }
        }
        const result = cleanupIds(comments, user.id, buildId, attempt);
        console.log(`Keeping automatic report ${result.keep}; removing ${result.remove.length} older reports.`);
        for (const id of result.remove) {
            const response = await client.del(
                `https://api.github.com/repos/${repository}/issues/comments/${id}`,
                { Authorization: `Bearer ${token}`, Accept: "application/vnd.github+json" }
            );
            await response.readBody();
            if (response.message.statusCode === 404) {
                console.log(`Report ${id} was already removed by another run.`);
            } else if (response.message.statusCode !== 204) {
                throw new Error(`Deleting report ${id} failed: HTTP ${response.message.statusCode}.`);
            } else {
                console.log(`Removed old automatic report ${id}.`);
            }
        }
    } finally {
        client.dispose();
    }
}

if (require.main === module) {
    run().then(
        () => tl.setResult(tl.TaskResult.Succeeded, "Old automatic reports cleaned."),
        (error: unknown) => tl.setResult(
            tl.TaskResult.Failed, error instanceof Error ? error.message : String(error)
        )
    );
}
