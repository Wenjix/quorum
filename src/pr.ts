export interface PullRequestRef {
  repo: string;
  pr: string;
  url: string;
}

export function parsePullRequestRef(input: string): PullRequestRef {
  const trimmed = input.trim();
  const githubUrl = trimmed.match(
    /^https?:\/\/github\.com\/([^/\s]+)\/([^/\s]+)\/pull\/([0-9]+)(?:[/?#].*)?$/i,
  );
  if (githubUrl) {
    const owner = githubUrl[1];
    const repoName = githubUrl[2];
    const pr = githubUrl[3];
    const repo = `${owner}/${repoName}`;
    return {
      repo,
      pr,
      url: `https://github.com/${repo}/pull/${pr}`,
    };
  }

  const shorthand = trimmed.match(/^([^/\s#]+)\/([^/\s#]+)#([0-9]+)$/);
  if (shorthand) {
    const repo = `${shorthand[1]}/${shorthand[2]}`;
    const pr = shorthand[3];
    return {
      repo,
      pr,
      url: `https://github.com/${repo}/pull/${pr}`,
    };
  }

  throw new Error(
    `Expected a GitHub PR URL like https://github.com/OWNER/REPO/pull/123, got: ${input}`,
  );
}

export function repoSlug(repo: string): string {
  return repo.replace(/[^A-Za-z0-9._-]+/g, "-");
}
