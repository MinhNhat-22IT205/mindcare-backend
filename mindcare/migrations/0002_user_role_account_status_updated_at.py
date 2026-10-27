import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('mindcare', '0002_alter_analysisrun_period_end_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='role',
            field=models.CharField(default='user', max_length=32),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='user',
            name='account_status',
            field=models.CharField(default='active', max_length=32),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='user',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
    ]
