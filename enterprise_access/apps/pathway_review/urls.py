"""
URLs for the pathway review bench.
"""

from django.urls import path

from enterprise_access.apps.pathway_review import views

app_name = 'pathway_review'

urlpatterns = [
    path('', views.bench, name='bench'),
    path('api/next/', views.next_item, name='next-item'),
    path('api/vote/', views.submit_vote, name='submit-vote'),
    path('api/goal/', views.set_goal, name='set-goal'),
    path('api/leaderboard/', views.leaderboard, name='leaderboard'),
]
