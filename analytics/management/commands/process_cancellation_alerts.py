from django.core.management.base import BaseCommand
from analytics.detection import process_pending

class Command(BaseCommand):
    help='Analyse les annulations en attente, sans sanction automatique.'
    def add_arguments(self,parser): parser.add_argument('--limit',type=int,default=200)
    def handle(self,*args,**options):
        count=process_pending(max(1,min(options['limit'],1000)))
        self.stdout.write(f'Annulations analysées : {count}')
