import os
import sys
from collections import namedtuple, defaultdict
from enum import Enum, auto

import requests
from github3 import login
from github3.pulls import ShortPullRequest
from github3.repos import status
from github3.repos.commit import ShortCommit


class CombinedBuildStatus:
    success = 'success'
    pending = 'pending'
    failure = 'failure'


class PullRequestStatusGroup(Enum):
    approved = auto()
    changes_requested = auto()
    mixed_reception = auto()
    unreviewed = auto()
    changes_since_review = auto()


class PullRequestReviewState:
    APPROVED = 'APPROVED'
    CHANGES_REQUESTED = 'CHANGES_REQUESTED'
    COMMENTED = 'COMMENTED'
    PENDING = 'PENDING'


POST_URL = 'https://slack.com/api/chat.postMessage'

ignore = os.environ.get('IGNORE_WORDS')
IGNORE_WORDS = [i.lower().strip() for i in ignore.split(',')] if ignore else []

repositories = os.environ.get('REPOSITORIES')
REPOSITORIES = [r.lower().strip() for r in repositories.split(',')] if repositories else []

usernames = os.environ.get('USERNAMES')
USERNAMES = [u.lower().strip() for u in usernames.split(',')] if usernames else []

SLACK_CHANNEL = os.environ.get('SLACK_CHANNEL', '#general')

ignore_build_status_contexts = os.environ.get('IGNORE_BUILD_STATUS_CONTEXTS')
IGNORE_BUILD_STATUS_CONTEXTS = [
    b.lower().strip() for b in ignore_build_status_contexts.split(',')] if ignore_build_status_contexts else []

SHOW_BUILD_STATUS = os.environ.get('SHOW_BUILD_STATUS', True)
SUCCESS_EMOJI = os.environ.get('SUCCESS_EMOJI', u'✓')
PENDING_EMOJI = os.environ.get('PENDING_EMOJI', u'⟳')
FAILURE_EMOJI = os.environ.get('FAILURE_EMOJI', u'⨉')

state_to_emoji = {
    CombinedBuildStatus.success: SUCCESS_EMOJI,
    CombinedBuildStatus.pending: PENDING_EMOJI,
    CombinedBuildStatus.failure: FAILURE_EMOJI,
    None: '       '  # for when there is no status for one PR but you still want the rest of the text to line up
}

SPLIT_BY_REVIEW_STATUS = os.environ.get('SPLIT_BY_REVIEW_STATUS', True)
APPROVED_GROUP_MSG = os.environ.get('APPROVED_GROUP_MSG', u':heart_eyes: _Approved_')
CHANGES_REQUESTED_GROUP_MSG = os.environ.get('CHANGES_REQUESTED_GROUP_MSG', u':thinking_face: _Changes Requested_')
MIXED_RECEPTION_GROUP_MSG = os.environ.get('MIXED_RECEPTION_GROUP_MSG', u':confounded: _Mixed Reception_')
UNREVIEWED_GROUP_MSG = os.environ.get('UNREVIEWED_GROUP_MSG', u':eyetwitch: _Needs Review_')
CHANGES_SINCE_REVIEW_GROUP_MSG = os.environ.get(
    'CHANGES_SINCE_REVIEW_GROUP_MSG', u':eyes: _Changes Since Last Review_')

pr_status_group_to_msg = {
    PullRequestStatusGroup.unreviewed: UNREVIEWED_GROUP_MSG,
    PullRequestStatusGroup.mixed_reception: MIXED_RECEPTION_GROUP_MSG,
    PullRequestStatusGroup.changes_requested: CHANGES_REQUESTED_GROUP_MSG,
    PullRequestStatusGroup.approved: APPROVED_GROUP_MSG,
    PullRequestStatusGroup.changes_since_review: CHANGES_SINCE_REVIEW_GROUP_MSG
}
pr_status_groups_display_order = [
    PullRequestStatusGroup.unreviewed,
    PullRequestStatusGroup.changes_since_review,
    PullRequestStatusGroup.mixed_reception,
    PullRequestStatusGroup.changes_requested,
    PullRequestStatusGroup.approved
]

# safety check
assert(set(pr_status_groups_display_order) == set(pr_status_group_to_msg.keys()))

try:
    SLACK_API_TOKEN = os.environ['SLACK_API_TOKEN']
    GITHUB_API_TOKEN = os.environ['GITHUB_API_TOKEN']
    ORGANIZATION = os.environ['ORGANIZATION']
except KeyError as error:
    sys.stderr.write('Please set the environment variable {0}'.format(error))
    sys.exit(1)

INITIAL_MESSAGE = """\
Hi! There's a few open pull requests you should take a \
look at:

"""


def fetch_repository_pulls(repository):
    pulls = []
    for pull in repository.pull_requests():
        if pull.state == 'open' and (not USERNAMES or pull.user.login.lower() in USERNAMES):
            pulls.append(pull)
    return pulls


def is_valid_title(title):
    lowercase_title = title.lower()
    for ignored_word in IGNORE_WORDS:
        if ignored_word in lowercase_title:
            return False

    return True


def fetch_combined_build_status(pull_request):
    """
    Can't use the github api pull request combined statuses endpoint as for some reason it combines review statuses
    and build statuses and we only want build statuses
    :param pull_request: github3.py PullRequest obj
    :return: a CombinedBuildStatus enum value or None if no statuses
    """
    build_statuses = fetch_pull_request_build_statuses(pull_request)

    # { status.context: most recently updated Status with that context}
    most_recent_status_by_context = {}

    for build_status in build_statuses:
        if build_status.updated_at is None:
            # gihub3.py seems to imply this can be None, so ignore those statuses
            continue

        if build_status.context not in most_recent_status_by_context:
            most_recent_status_by_context[build_status.context] = build_status
        elif build_status.updated_at > most_recent_status_by_context[build_status.context].updated_at:
            most_recent_status_by_context[build_status.context] = build_status

    for context_to_ingnore in IGNORE_BUILD_STATUS_CONTEXTS:
        if context_to_ingnore in most_recent_status_by_context:
            del most_recent_status_by_context[context_to_ingnore]

    most_recent_statuses = most_recent_status_by_context.values()

    if len(most_recent_statuses) == 0:
        # no statuses, return None
        return None

    any_pending = False
    for status in most_recent_statuses:
        if status.state == CombinedBuildStatus.failure:
            # if any failed, return overall failure
            return CombinedBuildStatus.failure
        if status.state == CombinedBuildStatus.pending:
            # can't return here in case there's a failed one after it
            any_pending = True
    return CombinedBuildStatus.pending if any_pending else CombinedBuildStatus.success


def fetch_pull_request_build_statuses(pull_request):
    """
    # TODO Remove after the PR adding this is merged into github3.py
    # PR: https://github.com/sigmavirus24/github3.py/pull/896
    Return iterator of all Statuses associated with head of this pull request.

     :param pull_request: PullRequest object
    :returns:
        generator of statuses for this pull request
    :rtype:
        :class:`~github3.repos.Status`
    """
    if pull_request.repository is None:
        return []
    url = pull_request._build_url(
        'statuses', pull_request.head.sha, base_url=pull_request.repository._api
    )
    return pull_request._iter(-1, url, status.Status)


class BetterShortRepoCommit(ShortCommit):
    """Representation of an incomplete commit in a collection."""

    class_name = 'Better Short Repository Commit'

    def _update_attributes(self, commit):
        super(BetterShortRepoCommit, self)._update_attributes(commit)
        commit_data = commit['commit']
        self.commit_date = self._strptime(commit_data['committer']['date'])


# patch this onto PullRequest.
def fetch_better_commits(pull_request, number=-1, etag=None):
    """Iterate over the commits on this pull request.

    :param int number:
        (optional), number of commits to return. Default: -1 returns all
        available commits.
    :param str etag:
        (optional), ETag from a previous request to the same endpoint
    :returns:
        generator of repository commit objects
    :rtype:
        :class:`~github3.repos.commit.ShortCommit`
    """
    url = pull_request._build_url('commits', base_url=pull_request._api)
    return pull_request._iter(int(number), url, BetterShortRepoCommit, etag=etag)


def fetch_pull_request_review_status_group(pull_request):
    reviews = pull_request.reviews()

    # sort by submit time, newest first
    reviews = sorted(reviews, key=lambda r: r.submitted_at, reverse=True)
    # remove author's review if there is one...
    reviews = filter(lambda r: r.user != pull_request.user, reviews)
    # remove comment reviews
    reviews = filter(lambda r: r.state != PullRequestReviewState.COMMENTED, reviews)
    # only keep last review by this reviewer
    users = set()
    last_reviews = []
    for review in reviews:
        if review.user in users:
            # if have a more recent review from this use, skip this one
            continue
        else:
            last_reviews.append(review)
            users.add(review.user)

    changes_requested = False
    approved = False
    for review in last_reviews:
        if review.state == PullRequestReviewState.CHANGES_REQUESTED:
            changes_requested = True
        elif review.state == PullRequestReviewState.APPROVED:
            approved = True

    if changes_requested:
        # sorted by date
        commits = sorted(fetch_better_commits(pull_request), key=lambda r: r.commit_date, reverse=True)

        if commits[0].commit_date > last_reviews[0].submitted_at:
            return PullRequestStatusGroup.changes_since_review
        if approved:
            return PullRequestStatusGroup.mixed_reception
        else:
            return PullRequestStatusGroup.changes_requested
    if approved:
        return PullRequestStatusGroup.approved

    return PullRequestStatusGroup.unreviewed


def format_pull_requests(pull_requests, owner):

    grouped_by_pr_status = defaultdict(list)

    for pull in pull_requests:
        if is_valid_title(pull.title):
            creator = pull.user.login
            combined_status = fetch_combined_build_status(pull)
            if SPLIT_BY_REVIEW_STATUS:
                pr_status_group = fetch_pull_request_review_status_group(pull)
            else:
                pr_status_group = 'default'
            if SHOW_BUILD_STATUS:
                build_status = state_to_emoji.get(combined_status, state_to_emoji[None])
            else:
                build_status = ""
            line = '*{}[{}/{}]* <{}|{} - by {}>'.format(
                build_status, owner, pull.repository.name, pull.html_url, pull.title, creator)
            grouped_by_pr_status[pr_status_group].append(line)

    if not SPLIT_BY_REVIEW_STATUS:
        return grouped_by_pr_status['default']

    output = []
    for group in pr_status_groups_display_order:
        lines = grouped_by_pr_status.get(group)
        if lines:
            output.append(pr_status_group_to_msg[group])
            output.extend(lines)

    return output


def fetch_organization_pulls(organization_name):
    """
    Returns a formatted string list of open pull request messages.
    """
    client = login(token=GITHUB_API_TOKEN)
    organization = client.organization(organization_name)
    lines = []

    prs = []
    for repository in organization.repositories():
        if REPOSITORIES and repository.name.lower() not in REPOSITORIES:
            continue
        unchecked_pulls = fetch_repository_pulls(repository)
        prs.extend(unchecked_pulls)

    lines = format_pull_requests(prs, organization_name)

    return lines


def send_to_slack(text):
    payload = {
        'token': SLACK_API_TOKEN,
        'channel': SLACK_CHANNEL,
        'username': 'Pull Request Reminder',
        'icon_emoji': ':bell:',
        'text': text
    }

    response = requests.post(POST_URL, data=payload)
    answer = response.json()
    if not answer['ok']:
        raise Exception(answer['error'])


def cli():
    lines = fetch_organization_pulls(ORGANIZATION)
    if lines:
        text = INITIAL_MESSAGE + '\n'.join(lines)
        send_to_slack(text)


if __name__ == '__main__':
    cli()
