# free-recipe-database

A public backup of freely-available recipes in a JSON

This an be exported in in its original JSON format, ORF, HTML, PDF, and the .paprikarecipes format. This should allow relatively simple import into most extant meal planning and grocery apps.

Included in each recipe are ingredients, equipment, instructions, a thumbnail picture, and complete nutritional info (including all macros and amino acids).

## Why did I make this?

I wanted to make sure that the recipes I've collected weren't lost or hidden behind bad apps, paywalls, or other enshittification barriers. I decided to compile all of them and store them here.

## What is included here?

 This repo consists of JSON-format recipe files including a thumbnail for each meal. The original JSON format provides very rich structured data about each recipe, including:

* equipment needed
* ingredients needed, including call-outs in the specific step where they are used
* instructions for cooking
* complete nutritional info, including macros and amino acids

Recipes are not scaled the way they are in many apps. For 2, 4, or 6 servings, there a separate recipe JSON file. The same goes for metric or US customary units--each got its own recipe. That means that each meal variant typically has six recipes associated with it. All of these are available here.

On top of that, recipes are grouped by *family*. A recipe family includes the classic version and any dietary variants such as gluten-free or vegetarian versions of the same recipe, where an ingredient is substituted. These are also all available here, in all scales, in both unit systems.

These JSON files serve as source data to allow export to open recipe format (ORF), 

### How are the recipes organized?

I used two systems for filtering recipes. One is *menu type*, which includes the following *positive filters* (recipes are *included* if one is selected):

1. Classic - includes most things
2. Vegetarian - no meat
3. Flexitarian - less meat, but still some
4. Pescatarian - vegetarian plus fish
5. Paleo - typically fewer grains
6. Vegan - no animal products
7. Low-carbohydrate - self explanatory
8. Keto - heavier on meat, low-carb

I also added *dietary restrictions* which are *negative filters* (recipes are *excluded* if one is selected):

1. GLuten-free
2. Dairy-free
3. No fish
4. No shellfish
5. Peanut-free
6. Tree-nut-free
7. Soy-free
8. Nightshade-free (tomatoes, eggplant, potatoes, and other things in the Solanaceae family)
9. Egg-free
10. Sesame-free
11. Mustard-free
12. Sulfite-free

The JSON files contain numeric tags which indicate which menu types each recipe is part of, and which dietary restrictions each recipe *violates*. 
