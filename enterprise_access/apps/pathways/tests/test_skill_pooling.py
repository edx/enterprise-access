"""
Tests for pooling a career family's skills from its members.
"""
from django.test import SimpleTestCase

from enterprise_access.apps.pathways.skill_pooling import POOLED_SKILL_LIMIT, pool_member_skills, skill_postings


def hit(*skills):
    """A jobs-index hit carrying ``(name, unique_postings)`` skills."""
    return {'name': 'Career', 'skills': [{'name': name, 'unique_postings': postings} for name, postings in skills]}


class TestSkillPostings(SimpleTestCase):
    """
    Scenario: A member's skills are read with how many of its postings ask for each.
    """

    def test_each_skill_carries_its_posting_count(self):
        self.assertEqual(skill_postings(hit(('SQL', 120.0), ('Excel', 40))), {'SQL': 120.0, 'Excel': 40.0})

    def test_a_missing_or_malformed_count_is_zero_not_a_dropped_skill(self):
        found = skill_postings({'skills': [
            {'name': 'SQL'}, {'name': 'Excel', 'unique_postings': 'many'}, {'name': 'R', 'unique_postings': -3},
        ]})

        self.assertEqual(found, {'SQL': 0.0, 'Excel': 0.0, 'R': 0.0})

    def test_malformed_entries_and_blank_names_are_skipped(self):
        found = skill_postings({'skills': ['SQL', {'unique_postings': 9}, {'name': '  '}, {'name': ' Python '}]})

        self.assertEqual(found, {'Python': 0.0})

    def test_a_skill_named_twice_counts_once_at_its_larger_figure(self):
        self.assertEqual(skill_postings(hit(('SQL', 10), ('SQL', 30))), {'SQL': 30.0})

    def test_no_hit_has_no_skills(self):
        self.assertEqual(skill_postings(None), {})


class TestPoolMemberSkills(SimpleTestCase):
    """
    Scenario: A family is searched with the skills its members' postings ask for most.
    """

    def test_skills_rank_by_postings_summed_across_members(self):
        pooled = pool_member_skills([
            hit(('Kubernetes', 400), ('Bloom Filter', 4)),
            hit(('Kubernetes', 300), ('Terraform', 500)),
        ])

        self.assertEqual(pooled, ['Kubernetes', 'Terraform', 'Bloom Filter'])

    def test_one_small_members_noise_is_outweighed_by_the_rest(self):
        """The round-4 shape: a 119-posting namesake beside its sales-development siblings."""
        pooled = pool_member_skills([
            hit(('Veterinary Pathology', 51), ('Animal Science', 117)),
            hit(('Lead Generation', 2000), ('Sales Process', 900)),
            hit(('Lead Generation', 700), ('Business Development', 600)),
        ], limit=3)

        self.assertEqual(pooled, ['Lead Generation', 'Sales Process', 'Business Development'])

    def test_a_tie_goes_to_the_skill_more_members_carry_then_to_the_name(self):
        pooled = pool_member_skills([
            hit(('Zeta', 10), ('Alpha', 20), ('Beta', 20)),
            hit(('Zeta', 10)),
        ])

        self.assertEqual(pooled, ['Zeta', 'Alpha', 'Beta'])

    def test_names_merge_across_members_whatever_their_case(self):
        pooled = pool_member_skills([hit(('Cyber Security', 5)), hit(('cyber security', 10), ('SQL', 12))])

        self.assertEqual(pooled, ['Cyber Security', 'SQL'])

    def test_members_the_index_does_not_hold_are_skipped(self):
        self.assertEqual(pool_member_skills([None, hit(('SQL', 1)), None]), ['SQL'])

    def test_the_list_is_cut_to_the_limit(self):
        many = hit(*[(f'Skill {index:02d}', 100 - index) for index in range(30)])

        self.assertEqual(len(pool_member_skills([many])), POOLED_SKILL_LIMIT)
        self.assertEqual(pool_member_skills([many], limit=2), ['Skill 00', 'Skill 01'])
        self.assertEqual(pool_member_skills([many], limit=0), [])

    def test_no_members_pool_no_skills(self):
        self.assertEqual(pool_member_skills([]), [])
