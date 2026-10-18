from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('RideVTC', '0030_ride_passenger_count_ride_ride_mode_and_more')]
    operations = [migrations.AddField(model_name='driverpresence', name='location_updated_at', field=models.DateTimeField(null=True, blank=True))]
