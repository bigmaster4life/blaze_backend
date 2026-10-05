from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("users", "0016_remove_customuser_must_reset_password")]
    operations = [
        migrations.AddField(model_name="customuser", name="email_verified", field=models.BooleanField(default=False)),
        migrations.CreateModel(
            name="EmailChallenge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phone_number", models.CharField(max_length=20)),
                ("email", models.EmailField(max_length=254)),
                ("purpose", models.CharField(max_length=16)),
                ("code_digest", models.CharField(max_length=64)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField()),
            ],
            options={"constraints": [models.UniqueConstraint(fields=("phone_number", "purpose"), name="unique_email_challenge")]},
        ),
    ]
