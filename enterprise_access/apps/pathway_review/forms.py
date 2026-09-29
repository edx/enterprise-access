"""
The queue upload form.

A round of review starts with a file somebody generated offline. Until now loading it took a
shell, which meant the people who run a round could not start one. This form is that file's
other door; :mod:`queue_loading` is what both doors call.
"""

from django import forms

#: Refuse anything larger before reading it. Round 2 of the shape review was 2.8 MB across 88
#: pathways, so this leaves room for a queue an order of magnitude bigger while still ruling out
#: the file that was picked by mistake.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class QueueUploadForm(forms.Form):
    """Upload a queue JSON file, optionally rehearsing it first."""

    queue_file = forms.FileField(
        label='Queue file',
        help_text='The JSON a pathway run produced: an object with a "ladders" list.',
    )
    dry_run = forms.BooleanField(
        label='Preview only',
        required=False,
        initial=True,
        help_text='Report what would change and load nothing. Clear this to load it for real.',
    )
    deactivate_missing = forms.BooleanField(
        label='Retire items this file leaves out',
        required=False,
        help_text=(
            'Take items absent from this file out of the queue. They are kept, along with any '
            'votes already cast on them, and simply stop being served.'
        ),
    )

    def clean_queue_file(self):
        """Refuse a file too large to be a queue before anything reads it."""
        uploaded = self.cleaned_data['queue_file']
        if uploaded.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError(
                f'That file is {uploaded.size / 1024 / 1024:.0f} MB; the limit is '
                f'{MAX_UPLOAD_BYTES // 1024 // 1024} MB.'
            )
        return uploaded
