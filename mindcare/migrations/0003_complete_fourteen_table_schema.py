import django.db.models.deletion
import django.utils.timezone
import uuid
from django.db import migrations, models


def populate_checkin_dates(apps, schema_editor):
    CheckIn = apps.get_model('mindcare', 'CheckIn')
    for check_in in CheckIn.objects.only('id', 'recorded_at').iterator():
        check_in.checkin_date = check_in.recorded_at.date()
        check_in.save(update_fields=['checkin_date'])


class Migration(migrations.Migration):

    dependencies = [
        ('mindcare', '0002_user_role_account_status_updated_at'),
    ]

    operations = [
        migrations.RenameField(
            model_name='user',
            old_name='account_status',
            new_name='status',
        ),
        migrations.RenameField(
            model_name='analysisreport',
            old_name='stress_trend',
            new_name='trend_metrics',
        ),
        migrations.RenameField(
            model_name='analysisreport',
            old_name='risk_level',
            new_name='concern_level',
        ),
        migrations.AlterField(
            model_name='analysisreport',
            name='trend_metrics',
            field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name='user',
            name='password_hash',
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AddField(
            model_name='user',
            name='reminder_enabled',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='user',
            name='reminder_time',
            field=models.CharField(blank=True, max_length=32),
        ),
        migrations.AddField(
            model_name='checkin',
            name='checkin_date',
            field=models.DateField(null=True),
        ),
        migrations.RunPython(populate_checkin_dates, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='checkin',
            name='checkin_date',
            field=models.DateField(),
        ),
        migrations.AddField(
            model_name='checkin',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='journalentry',
            name='updated_at',
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='analysisrun',
            name='trigger_source',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='analysisrun',
            name='pipeline_version',
            field=models.CharField(blank=True, max_length=128),
        ),
        migrations.AddField(
            model_name='analysisreport',
            name='contributing_factors',
            field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name='safetyalert',
            name='trigger_type',
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name='safetyalert',
            name='reviewed_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='safetyalert',
            name='reviewer_user',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='reviewed_safety_alerts', to='mindcare.user'),
        ),
        migrations.CreateModel(
            name='UserConsent',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('scope', models.CharField(max_length=64)),
                ('action', models.CharField(max_length=32)),
                ('policy_version', models.CharField(max_length=64)),
                ('recorded_at', models.DateTimeField(auto_now_add=True)),
                ('expires_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='consents', to='mindcare.user')),
            ],
            options={'db_table': 'user_consents'},
        ),
        migrations.CreateModel(
            name='TrustedContact',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('display_name', models.CharField(max_length=255)),
                ('relationship', models.CharField(max_length=64)),
                ('contact_type', models.CharField(max_length=32)),
                ('contact_value_encrypted', models.TextField()),
                ('verification_status', models.CharField(max_length=32)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='trusted_contacts', to='mindcare.user')),
            ],
            options={'db_table': 'trusted_contacts'},
        ),
        migrations.CreateModel(
            name='SafetyPlan',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('coping_steps', models.JSONField(default=list)),
                ('personal_notes', models.TextField(blank=True)),
                ('enabled', models.BooleanField(default=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='safety_plan', to='mindcare.user')),
            ],
            options={'db_table': 'safety_plans'},
        ),
        migrations.CreateModel(
            name='AlertDelivery',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('channel', models.CharField(max_length=32)),
                ('delivery_status', models.CharField(max_length=32)),
                ('sent_at', models.DateTimeField(blank=True, null=True)),
                ('delivered_at', models.DateTimeField(blank=True, null=True)),
                ('error_code', models.CharField(blank=True, max_length=64)),
                ('alert', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='deliveries', to='mindcare.safetyalert')),
                ('recipient_user', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='received_alert_deliveries', to='mindcare.user')),
                ('trusted_contact', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='alert_deliveries', to='mindcare.trustedcontact')),
            ],
            options={'db_table': 'alert_deliveries'},
        ),
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('notification_type', models.CharField(max_length=64)),
                ('title', models.CharField(max_length=255)),
                ('safe_message', models.TextField()),
                ('is_read', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('safety_alert', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='notifications', to='mindcare.safetyalert')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to='mindcare.user')),
            ],
            options={'db_table': 'notifications'},
        ),
    ]
