""" Admin for the pathway review bench. """

from django.contrib import admin
from django.db.models import Count

from enterprise_access.apps.pathway_review.models import PathwayReviewerProfile, PathwayReviewItem, PathwayReviewVote


@admin.register(PathwayReviewItem)
class PathwayReviewItemAdmin(admin.ModelAdmin):
    """Browse the queue. The blinding fields are visible here because admins are not reviewers."""

    list_display = ('item_id', 'pathway', 'pool', 'mix', 'careers_covered', 'is_active', 'vote_count')
    list_filter = ('pool', 'mix', 'is_active')
    search_fields = ('item_id', 'pathway', 'family_key')
    readonly_fields = ('payload', 'control_key')

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_votes=Count('votes'))

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
