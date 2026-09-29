""" Admin for the pathway review bench. """

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from enterprise_access.apps.pathway_review.forms import QueueUploadForm
from enterprise_access.apps.pathway_review.models import PathwayReviewerProfile, PathwayReviewItem, PathwayReviewVote
from enterprise_access.apps.pathway_review.queue_loading import PROBLEMS_SHOWN, QueueError, load_queue_file


@admin.register(PathwayReviewItem)
class PathwayReviewItemAdmin(admin.ModelAdmin):
    """Browse the queue. The blinding fields are visible here because admins are not reviewers."""

    list_display = ('item_id', 'pathway', 'pool', 'mix', 'careers_covered', 'is_active', 'vote_count')
    list_filter = ('pool', 'mix', 'is_active')
    search_fields = ('item_id', 'pathway', 'family_key')
    readonly_fields = ('payload', 'control_key')
    change_list_template = 'admin/pathway_review/pathwayreviewitem/change_list.html'

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_votes=Count('votes'))

    def get_urls(self):
        """The upload page, under this model so its permissions are the model's."""
        return [
            path(
                'upload/',
                self.admin_site.admin_view(self.upload_queue),
                name='pathway_review_pathwayreviewitem_upload',
            ),
        ] + super().get_urls()

    def upload_queue(self, request):
        """
        Load a queue file, or rehearse loading it.

        A round of review begins with a file produced offline. Whoever runs the round should be
        able to start it without a shell, which is all this page is: the same loader the
        management command calls, behind the permission that already governs adding items.
        """
        if not self.has_add_permission(request):
            raise PermissionDenied

        form = QueueUploadForm(request.POST or None, request.FILES or None)
        report = None
        if request.method == 'POST' and form.is_valid():
            try:
                report = load_queue_file(
                    form.cleaned_data['queue_file'].read(),
                    deactivate_missing=form.cleaned_data['deactivate_missing'],
                    dry_run=form.cleaned_data['dry_run'],
                )
            except QueueError as exc:
                form.add_error('queue_file', str(exc))
                for problem in exc.problems[:PROBLEMS_SHOWN]:
                    form.add_error(None, problem)
                if len(exc.problems) > PROBLEMS_SHOWN:
                    form.add_error(None, f'... and {len(exc.problems) - PROBLEMS_SHOWN} more.')
            else:
                if report['dry_run']:
                    messages.info(request, format_html(
                        'Preview only, nothing was loaded: <b>{}</b> new, <b>{}</b> updated, '
                        '<b>{}</b> would be retired.',
                        report['created'], report['updated'], report['would_deactivate'],
                    ))
                else:
                    messages.success(request, format_html(
                        '<b>{}</b> new, <b>{}</b> updated, <b>{}</b> retired. '
                        '<b>{}</b> pathways are now in the queue.',
                        report['created'], report['updated'], report['deactivated'], report['active'],
                    ))
                    if report['deactivated_with_votes']:
                        messages.warning(request, format_html(
                            '{} retired pathway(s) already carried votes. The votes are kept, but '
                            'those pathways will no longer be served.',
                            report['deactivated_with_votes'],
                        ))
                    return redirect(reverse('admin:pathway_review_pathwayreviewitem_changelist'))

        return render(request, 'admin/pathway_review/upload_queue.html', {
            **self.admin_site.each_context(request),
            'title': 'Upload a review queue',
            'opts': self.model._meta,
            'form': form,
            'report': report,
        })

    @admin.display(description='Votes', ordering='_votes')
    def vote_count(self, obj):
        return obj._votes  # pylint: disable=protected-access


@admin.register(PathwayReviewVote)
class PathwayReviewVoteAdmin(admin.ModelAdmin):
    """ Collected judgements. """

    list_display = ('item', 'reviewer', 'verdict', 'seconds', 'created')
    list_filter = ('verdict', 'item__pool')
    search_fields = ('item__item_id', 'item__pathway', 'reviewer__username')
    raw_id_fields = ('item', 'reviewer')


@admin.register(PathwayReviewerProfile)
class PathwayReviewerProfileAdmin(admin.ModelAdmin):
    """ Reviewer goals. """

    list_display = ('user', 'goal')
    raw_id_fields = ('user',)
